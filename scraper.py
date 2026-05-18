# scraper.py
# Core module for extracting market intelligence from the web.

import requests # HTTP library for making web requests
from bs4 import BeautifulSoup # HTML parsing library
import os # Operating system interface (not heavily used here but good for paths)
import time # Time module for adding delays
import random # Random module for generating variable delays
from datetime import datetime, timedelta # Date parsing and manipulation
from models import Event # Import the SQLAlchemy Event model
from fake_useragent import UserAgent # Library to spoof browser user-agents
from tenacity import retry, stop_after_attempt, wait_exponential # Robust retry logic

# Initialize User-Agent rotator to prevent fingerprinting
ua = UserAgent()

# Market Intelligence Dictionary for common events
# Provides professional financial context for raw data points
INTELLIGENCE_DICT = {
    'CPI': 'Measures the change in the price of goods and services from the perspective of the consumer. It is a key way to measure changes in purchasing trends and inflation.',
    'Non-Farm Employment Change': 'Measures the change in the number of employed people during the previous month, excluding the farming industry. Job creation is an important leading indicator of consumer spending.',
    'Unemployment Rate': 'Measures the percentage of the total workforce that is unemployed and actively seeking employment during the previous month.',
    'FOMC Statement': 'The primary tool the FOMC uses to communicate with investors regarding monetary policy. It contains the outcome of their vote on interest rates and other policy measures.',
    'Federal Funds Rate': 'The interest rate at which depository institutions lend balances at the Federal Reserve to other depository institutions overnight.',
    'GDP': 'The broadest measure of economic activity and the primary indicator of the economy\'s health.',
    'Retail Sales': 'A primary measure of consumer spending, which accounts for the majority of overall economic activity.',
    'PMI': 'A survey of purchasing managers in the manufacturing/services sector. A reading above 50 indicates expansion; below 50 indicates contraction.',
    'Interest Rate': 'The amount charged, expressed as a percentage of principal, by a lender to a borrower for the use of assets.',
}

# The retry decorator ensures the function runs up to 3 times if it fails
# It uses an exponential backoff (waits 2s, 4s, etc.) to avoid spamming the server
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def fetch_page(url):
    # Construct headers to mimic a real human using a modern browser
    headers = {
        'User-Agent': ua.random, # Pick a random browser identity
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8', # Standard accept headers
        'Accept-Language': 'en-US,en;q=0.5', # Pretend to be an English speaker
        'Referer': 'https://www.google.com/', # Pretend we came from Google
        'DNT': '1', # Do Not Track request
        'Connection': 'keep-alive', # Keep connection open
        'Upgrade-Insecure-Requests': '1' # Standard security header
    }
    # Execute the GET request
    response = requests.get(url, headers=headers, timeout=15)
    # Raise an exception if the response code is an error (e.g., 404, 500)
    response.raise_for_status()
    # Return the raw HTML text
    return response.text

# Helper function to categorize a given time string into a major market "Killzone"
def get_killzone(t):
    if not t: return 'Outside' # If no time is provided, return default
    t = t.lower() # Normalize string
    
    # Process morning (AM) times
    if 'am' in t:
        try:
            hour = int(t.split(':')[0]) # Extract the hour
            if hour == 12: hour = 0 # Handle 12 AM edge case
            if 2 <= hour <= 5: return 'London Open' # 2am-5am is London Open overlap
            if 7 <= hour <= 10: return 'NY Open' # 7am-10am is NY Open overlap
        except: pass # Ignore parsing errors
        
    # Process afternoon/evening (PM) times
    elif 'pm' in t:
        try:
            parts = t.split(':') # Split by colon
            hour = int(parts[0]) # Extract the hour
            if hour == 12: hour = 12 # Handle 12 PM edge case
            else: hour += 12 # Convert to 24-hour format
            if 13 <= hour <= 15: return 'NY Open' # 1pm-3pm overlap
        except: pass # Ignore parsing errors
        
    return 'Outside' # Default return if no specific zone matched

# Main extraction engine function (Optimised with Parallel Concurrency)
def scrape_forex_calendar(start_date=None, end_date=None, currencies=None, impacts=None, asset_type='currency', scan_id=None, db_session=None, progress_callback=None):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    # The base URL we are targeting
    base_url = "https://www.forexfactory.com/calendar?day="
    
    # Safely parse the user-provided date strings into datetime objects
    try:
        start = datetime.strptime(start_date, '%Y-%m-%d')
        end = datetime.strptime(end_date, '%Y-%m-%d')
    except:
        # Fallback to current week if dates are invalid
        start = datetime.now()
        end = start + timedelta(days=7)
    
    # Build a list of all date objects to process
    dates_to_fetch = []
    current_date = start
    while current_date <= end:
        dates_to_fetch.append(current_date)
        current_date += timedelta(days=1)
        
    total_days = len(dates_to_fetch)
    if total_days == 0:
        return True

    # Lock and counter for thread-safe progress updating during parallel fetching
    progress_lock = threading.Lock()
    fetched_days_count = 0
    html_results = {}

    # Worker thread task to fetch HTML for a single day
    def fetch_day(date_obj):
        nonlocal fetched_days_count
        date_str = date_obj.strftime("%b%d.%Y").lower()
        target_url = f"{base_url}{date_str}"
        
        # Add a very small random delay (jitter) to prevent concurrency spikes
        time.sleep(random.uniform(0.05, 0.2))
        
        try:
            html = fetch_page(target_url)
            with progress_lock:
                fetched_days_count += 1
                # Scale fetching progress from 0% to 50%
                percent = int((fetched_days_count / total_days) * 50)
                if progress_callback:
                    progress_callback(percent, f"Fetched data for {date_obj.strftime('%b %d')}...")
            return date_obj, html
        except Exception as e:
            with progress_lock:
                fetched_days_count += 1
                percent = int((fetched_days_count / total_days) * 50)
                if progress_callback:
                    progress_callback(percent, f"Failed fetching {date_obj.strftime('%b %d')}...")
            print(f"Error scraping {date_str} after retries: {e}")
            return date_obj, None

    # Fetch pages concurrently using ThreadPoolExecutor (highly scalable I/O bottleneck fix)
    # We use a balanced worker pool of 5 to remain server-friendly and avoid rate-limiting
    max_workers = min(5, total_days)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all days to the thread pool
        futures = {executor.submit(fetch_day, d): d for d in dates_to_fetch}
        for future in futures:
            date_obj, html = future.result()
            if html:
                html_results[date_obj] = html

    # Process and parse HTML results in chronological order (maintains SQL index consistency)
    chronological_dates = sorted(html_results.keys())
    parsed_days_count = 0
    total_fetched = len(chronological_dates)

    for date_obj in chronological_dates:
        html = html_results[date_obj]
        date_str = date_obj.strftime("%b%d.%Y").lower()
        
        parsed_days_count += 1
        # Scale parsing & storage progress from 50% to 100%
        percent = 50 + int((parsed_days_count / max(1, total_fetched)) * 50)
        if progress_callback:
            progress_callback(percent, f"Parsing & Storing {date_obj.strftime('%b %d')}...")

        try:
            # Parse the HTML structure
            soup = BeautifulSoup(html, 'html.parser')
            # Select all event rows, excluding the date breaker rows
            rows = soup.select('.calendar__row:not(.calendar__row--day-breaker)')
            
            # Iterate through each row found on the page
            for row in rows:
                event_title_el = row.select_one('.calendar__event-title')
                if event_title_el:
                    time_val = row.select_one('.calendar__time').text.strip() if row.select_one('.calendar__time') else ''
                    currency = row.select_one('.calendar__currency').text.strip() if row.select_one('.calendar__currency') else ''
                    impact = 'High' if 'impact-red' in str(row) else ('Medium' if 'impact-orange' in str(row) else 'Low')
                    event_title = event_title_el.text.strip()

                    detail_url_base = "https://www.forexfactory.com/"
                    url_el = row.select_one('.calendar__detail-link')
                    detail_link = detail_url_base + url_el.get('href') if url_el and url_el.get('href') else None

                    # Apply filters
                    if asset_type == 'gold':
                        is_gold_driver = "gold" in event_title.lower() or "xau" in event_title.lower() or currency == 'USD'
                        if not is_gold_driver: continue
                        if impacts and impact not in impacts: continue
                        
                        if "gold" not in event_title.lower() and currency == 'USD':
                            event_title = f"[XAU Driver] {event_title}"
                    elif asset_type == 'currency':
                        if currencies and currency not in currencies: continue
                        if impacts and impact not in impacts: continue
                    else:
                        if impacts and impact not in impacts: continue
                        if currencies:
                            currency = random.choice(currencies)

                    # Determine Explanation
                    explanation = "Standard economic release affecting market liquidity and volatility."
                    for key, desc in INTELLIGENCE_DICT.items():
                        if key.lower() in event_title.lower():
                            explanation = desc
                            break
                    
                    if asset_type == 'gold' and currency == 'USD':
                        explanation = f"High priority Gold driver. {explanation}"

                    if detail_link:
                        explanation += f"<br><br><a href='{detail_link}' target='_blank' style='color: var(--primary); text-decoration: underline;'><i class='fas fa-external-link-alt'></i> View Official Source Data</a>"

                    # NLP Sentiment Tagging
                    tags = []
                    title_lower = event_title.lower()
                    
                    if any(kw in title_lower for kw in ['rate', 'statement', 'fomc', 'minutes', 'bank']):
                        tags.append('#MonetaryPolicy')
                    if any(kw in title_lower for kw in ['cpi', 'ppi', 'inflation', 'price']):
                        tags.append('#Inflation')
                    if any(kw in title_lower for kw in ['employment', 'job', 'payroll', 'unemployment', 'claims']):
                        tags.append('#LaborMarket')
                    if any(kw in title_lower for kw in ['gdp', 'sales', 'pmi', 'manufacturing', 'services', 'confidence']):
                        tags.append('#EconomicGrowth')
                    if asset_type == 'gold' and currency == 'USD':
                        tags.append('#GoldDriver')
                    
                    tags_str = ','.join(tags)

                    # Create DB event record
                    new_event = Event(
                        scan_id=scan_id,
                        date=date_obj.strftime("%Y-%m-%d"),
                        time=time_val,
                        currency=currency if asset_type == 'currency' else 'XAU/USD',
                        impact=impact,
                        event_title=event_title,
                        explanation=explanation,
                        killzone=get_killzone(time_val),
                        tags=tags_str
                    )
                    db_session.add(new_event)
            
            # Commit sequentially on the main Celery thread to maintain SQL context safety
            if db_session:
                db_session.commit()
                
        except Exception as e:
            print(f"Error parsing {date_str}: {e}")
                  
    return True
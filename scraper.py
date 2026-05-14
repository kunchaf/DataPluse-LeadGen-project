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

# Main extraction engine function
def scrape_forex_calendar(start_date=None, end_date=None, currencies=None, impacts=None, asset_type='currency', scan_id=None, db_session=None, progress_callback=None):
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
    
    # Calculate the total number of days to process for the progress bar
    total_days = (end - start).days + 1
    current_date = start # Initialize the loop counter
    days_processed = 0 # Track progress
    
    # Loop through each day in the date range
    while current_date <= end:
        # Format the date to match Forex Factory's URL structure (e.g., 'may13.2026')
        date_str = current_date.strftime("%b%d.%Y").lower()
        target_url = f"{base_url}{date_str}" # Build the full URL
        
        # Increment progress and report back to the frontend UI
        days_processed += 1
        percent = int((days_processed / total_days) * 100) # Calculate percentage
        if progress_callback:
            progress_callback(percent, f"Synchronizing {current_date.strftime('%b %d')}...")

        # Add a random human delay between 1 and 2.5 seconds to prevent rate-limiting
        time.sleep(random.uniform(1.0, 2.5))
        
        try:
            # Fetch the HTML using our resilient fetcher
            html = fetch_page(target_url)
            # Parse the HTML structure
            soup = BeautifulSoup(html, 'html.parser')
            # Select all event rows, excluding the date breaker rows
            rows = soup.select('.calendar__row:not(.calendar__row--day-breaker)')
            
            # Iterate through each row found on the page
            for row in rows:
                # Find the element containing the event title
                event_title_el = row.select_one('.calendar__event-title')
                
                # If an event title exists, process the row
                if event_title_el:
                    # Extract the time, or empty string if missing
                    time_val = row.select_one('.calendar__time').text.strip() if row.select_one('.calendar__time') else ''
                    # Extract the currency, or empty string if missing
                    currency = row.select_one('.calendar__currency').text.strip() if row.select_one('.calendar__currency') else ''
                    # Determine impact based on the CSS class of the icon
                    impact = 'High' if 'impact-red' in str(row) else ('Medium' if 'impact-orange' in str(row) else 'Low')
                    # Clean up the event title text
                    event_title = event_title_el.text.strip()

                    # --- Extract Detail URL ---
                    # The detail is also fetched from the URL. We find the 'a' tag.
                    detail_url_base = "https://www.forexfactory.com/"
                    url_el = row.select_one('.calendar__detail-link')
                    detail_link = detail_url_base + url_el.get('href') if url_el and url_el.get('href') else None

                    # --- Apply specialized filters ---
                    # If the user selected the 'Gold' path
                    if asset_type == 'gold':
                        # Check if the event drives Gold prices
                        is_gold_driver = "gold" in event_title.lower() or "xau" in event_title.lower() or currency == 'USD'
                        if not is_gold_driver: continue # Skip if irrelevant
                        if impacts and impact not in impacts: continue # Apply impact filter
                        
                        # Add a visual prefix to USD events that affect Gold
                        if "gold" not in event_title.lower() and currency == 'USD':
                            event_title = f"[XAU Driver] {event_title}"
                    else:
                        # Standard currency path: check if currency matches user selection
                        if currencies and currency not in currencies: continue
                        # Standard currency path: check if impact matches user selection
                        if impacts and impact not in impacts: continue

                    # --- Determine Intelligence Explanation ---
                    # Set a baseline generic explanation
                    explanation = "Standard economic release affecting market liquidity and volatility."
                    # Cross-reference with our Intelligence Dictionary
                    for key, desc in INTELLIGENCE_DICT.items():
                        if key.lower() in event_title.lower():
                            explanation = desc # Update explanation if match found
                            break
                    
                    # Add specialized warning for Gold
                    if asset_type == 'gold' and currency == 'USD':
                        explanation = f"High priority Gold driver. {explanation}"

                    # Append the official detail URL to the explanation if we found it
                    if detail_link:
                        explanation += f"<br><br><a href='{detail_link}' target='_blank' style='color: var(--primary); text-decoration: underline;'><i class='fas fa-external-link-alt'></i> View Official Source Data</a>"

                    # --- NLP Sentiment Tagging ---
                    tags = [] # Initialize empty tags list
                    title_lower = event_title.lower() # Lowercase for matching
                    
                    # Check for monetary policy keywords
                    if any(kw in title_lower for kw in ['rate', 'statement', 'fomc', 'minutes', 'bank']):
                        tags.append('#MonetaryPolicy')
                    # Check for inflation keywords
                    if any(kw in title_lower for kw in ['cpi', 'ppi', 'inflation', 'price']):
                        tags.append('#Inflation')
                    # Check for labor market keywords
                    if any(kw in title_lower for kw in ['employment', 'job', 'payroll', 'unemployment', 'claims']):
                        tags.append('#LaborMarket')
                    # Check for growth keywords
                    if any(kw in title_lower for kw in ['gdp', 'sales', 'pmi', 'manufacturing', 'services', 'confidence']):
                        tags.append('#EconomicGrowth')
                    # Add gold driver tag if applicable
                    if asset_type == 'gold' and currency == 'USD':
                        tags.append('#GoldDriver')
                    
                    # Join tags into a single comma-separated string for DB storage
                    tags_str = ','.join(tags)

                    # --- Create database record ---
                    # Instantiate the SQLAlchemy Event object
                    new_event = Event(
                        scan_id=scan_id, # Link to parent scan
                        date=current_date.strftime("%Y-%m-%d"), # Store formatted date
                        time=time_val, # Store time
                        currency=currency if asset_type == 'currency' else 'XAU/USD', # Store currency
                        impact=impact, # Store impact level
                        event_title=event_title, # Store processed title
                        explanation=explanation, # Store intelligent explanation (with HTML link)
                        killzone=get_killzone(time_val), # Store calculated killzone
                        tags=tags_str # Store NLP tags
                    )
                    # Add the new object to the SQLAlchemy session
                    db_session.add(new_event)
            
            # Commit the session after each successful day's processing
            if db_session:
                db_session.commit()
                
        except Exception as e:
            # Print error if the resilient fetcher fails after all retries
            print(f"Error scraping {date_str} after retries: {e}")
        
        # Advance the loop counter to the next day
        current_date += timedelta(days=1)
                 
    # Return True when the entire date range is completed
    return True
import os
from flask import Flask, render_template, request, redirect, url_for, send_file, Response
import csv
import io
from celery import Celery
from models import db, Scan, Event
from scraper import scrape_forex_calendar

app = Flask(__name__)

# Database Configuration
app.config['SECRET_KEY'] = 'your-super-secret-key-change-in-production'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///intelligence.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Celery Configuration (Optimised for Zero-Install on Windows)
# To use Redis, change to 'redis://localhost:6379/0'
app.config['CELERY_BROKER_URL'] = 'sqla+sqlite:///data/celery_broker.db'
app.config['CELERY_RESULT_BACKEND'] = 'db+sqlite:///data/celery_broker.db'

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)

db.init_app(app)

# Create database tables
with app.app_context():
    db.create_all()

@app.route('/')
def index():
    return render_template('Index.html')

@celery.task(bind=True)
def background_scrape(self, start_date, end_date, currencies, impacts, asset_type, scan_id):
    def update_progress(percent, status):
        self.update_state(state='PROGRESS', meta={'percent': percent, 'status': status, 'scan_id': scan_id})

    with app.app_context():
        scrape_forex_calendar(
            start_date=start_date, 
            end_date=end_date, 
            currencies=currencies, 
            impacts=impacts, 
            asset_type=asset_type,
            scan_id=scan_id,
            db_session=db.session,
            progress_callback=update_progress
        )
    return {'percent': 100, 'status': 'Completed', 'scan_id': scan_id}

@app.route('/run', methods=['POST'])
def run():
    scan_name = request.form.get("scan_name", "Latest_Sync")
    start_date = request.form.get("start_date")
    end_date = request.form.get("end_date")
    asset_type = request.form.get("asset_type", "currency")
    impacts = request.form.getlist("impact")
    currencies = request.form.getlist("currencies") if asset_type == 'currency' else ['USD']
    
    new_scan = Scan(name=scan_name, start_date=start_date, end_date=end_date, asset_type=asset_type)
    db.session.add(new_scan)
    db.session.commit()
    
    # Push to Celery
    task = background_scrape.delay(start_date, end_date, currencies, impacts, asset_type, new_scan.id)
    
    return redirect(url_for('loading', task_id=task.id, scan_id=new_scan.id))

@app.route('/loading/<task_id>/<int:scan_id>')
def loading(task_id, scan_id):
    return render_template('loading.html', task_id=task_id, scan_id=scan_id)

@app.route('/status/<task_id>')
def task_status(task_id):
    task = background_scrape.AsyncResult(task_id)
    response = {'state': task.state}
    
    if task.state == 'PROGRESS':
        response.update({
            'status': task.info.get('status', 'Synchronizing...'),
            'percent': task.info.get('percent', 0),
            'scan_id': task.info.get('scan_id', '')
        })
    elif task.state == 'SUCCESS':
        response.update({
            'status': 'Completed',
            'percent': 100,
            'scan_id': task.info.get('scan_id', '')
        })
    elif task.state == 'FAILURE':
        response.update({
            'status': 'Synchronization Failed',
            'percent': 0,
            'error': str(task.info)
        })
    else:
        response.update({
            'status': 'Pending...',
            'percent': 0
        })
        
    return response

@app.route('/dashboard')
def dashboard():
    # Fetch all scans for the history dropdown
    all_scans = Scan.query.order_by(Scan.timestamp.desc()).all()
    
    # Determine which scan to display
    scan_id = request.args.get('scan_id', type=int)
    selected_scan = None
    
    if scan_id:
        selected_scan = Scan.query.get(scan_id)
    elif all_scans:
        selected_scan = all_scans[0]
    
    leads = selected_scan.events if selected_scan else []
    
    # Calculate Volatility Heatmap Data
    volatility_data = {}
    if leads:
        for event in leads:
            curr = event.currency
            if curr not in volatility_data:
                volatility_data[curr] = {'High': 0, 'Medium': 0, 'Low': 0, 'score': 0}
            
            impact = event.impact
            volatility_data[curr][impact] += 1
            
            # Weighted score for heatmap intensity
            if impact == 'High': volatility_data[curr]['score'] += 3
            elif impact == 'Medium': volatility_data[curr]['score'] += 1
    
    # Sort by score descending
    sorted_volatility = dict(sorted(volatility_data.items(), key=lambda item: item[1]['score'], reverse=True))

    return render_template('dashboard.html', 
                           leads=leads, 
                           all_scans=all_scans, 
                           volatility=sorted_volatility,
                           current_scan_id=selected_scan.id if selected_scan else None,
                           current_scan_name=selected_scan.name if selected_scan else "No Active Sync")

@app.route('/download/<int:scan_id>')
def download(scan_id):
    scan = Scan.query.get_or_404(scan_id)
    
    # Generate CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Date', 'Time', 'Currency', 'Impact', 'Event', 'Killzone'])
    
    for event in scan.events:
        writer.writerow([event.date, event.time, event.currency, event.impact, event.event_title, event.killzone])
    
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-disposition": f"attachment; filename={scan.name.replace(' ', '_')}.csv"}
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
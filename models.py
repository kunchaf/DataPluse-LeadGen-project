from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Scan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    start_date = db.Column(db.String(20))
    end_date = db.Column(db.String(20))
    asset_type = db.Column(db.String(20))
    
    # Relationship to events
    events = db.relationship('Event', backref='scan', lazy=True, cascade="all, delete-orphan")

    def __init__(self, **kwargs):
        super(Scan, self).__init__(**kwargs)

    def __repr__(self):
        return f'<Scan {self.name}>'

class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    scan_id = db.Column(db.Integer, db.ForeignKey('scan.id'), nullable=False)
    date = db.Column(db.String(20))
    time = db.Column(db.String(20))
    currency = db.Column(db.String(20))
    impact = db.Column(db.String(20))
    event_title = db.Column(db.String(200))
    explanation = db.Column(db.Text)
    killzone = db.Column(db.String(50))
    tags = db.Column(db.String(100))

    def __init__(self, **kwargs):
        super(Event, self).__init__(**kwargs)

    def __repr__(self):
        return f'<Event {self.event_title}>'

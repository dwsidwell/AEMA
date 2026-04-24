import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import requests
from bs4 import BeautifulSoup
import datetime
import time
import logging
import logging_loki
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'default-secret-key')

# Grafana Loki Setup
logger = logging.getLogger("iamresponding-viewer")
logger.setLevel(logging.INFO)
loki_url = os.environ.get('GRAFANA_LOKI_URL')
loki_username = os.environ.get('GRAFANA_LOKI_USERNAME')
loki_password = os.environ.get('GRAFANA_LOKI_PASSWORD')

if loki_url and loki_username:
    handler = logging_loki.LokiHandler(
        url=loki_url,
        tags={"application": "iamresponding-viewer"},
        auth=(loki_username, loki_password),
        version="1",
    )
    logger.addHandler(handler)
else:
    logging.basicConfig(level=logging.INFO)
    logger.warning("Grafana Loki credentials not fully configured. Logging to console only.")

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form.get('password') == os.environ.get('SITE_PASSWORD'):
            session['authenticated'] = True
            logger.info("Successful login attempt", extra={"tags": {"event_type": "app_telemetry", "action": "login_success"}})
            return redirect(url_for('index'))
        else:
            error = 'Invalid password'
            logger.warning("Failed login attempt", extra={"tags": {"event_type": "app_telemetry", "action": "login_failed"}})
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('authenticated', None)
    return redirect(url_for('login'))

@app.route('/')
def index():
    if not session.get('authenticated'):
        return redirect(url_for('login'))
    return render_template('index.html')

@app.route('/api/events', methods=['POST'])
def get_events():
    if not session.get('authenticated'):
        logger.warning("Unauthorized access attempt to /api/events", extra={"tags": {"event_type": "app_telemetry"}})
        return jsonify({'error': 'Unauthorized. Please login again.'}), 401

    data = request.json
    agency = data.get('agency')
    username = data.get('username')
    password = data.get('password')
    days = data.get('days', 10)

    if not all([agency, username, password]):
        logger.error("Scrape failed: Missing credentials", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
        return jsonify({'error': 'Missing credentials'}), 400

    logger.info(f"Starting scrape for agency: {agency}, days: {days}", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_started"}})
    scrape_start_time = time.time()
    req_session = requests.Session()
    req_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
    })

    urls_called = []

    try:
        # Step 1: Get the login page to retrieve the anti-forgery token
        login_url = "https://auth.iamresponding.com/login/member"
        urls_called.append(f"GET {login_url}")
        get_response = req_session.get(login_url)
        get_response.raise_for_status()

        soup = BeautifulSoup(get_response.text, 'html.parser')
        token_input = soup.find('input', {'name': '__RequestVerificationToken'})
        
        if not token_input:
            logger.error("Scrape failed: Could not find RequestVerificationToken", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
            return jsonify({'error': 'Could not find RequestVerificationToken on login page.'}), 500
        
        token = token_input.get('value')

        # Step 2: Post the login credentials
        login_data = {
            'Input.Agency': agency,
            'Input.Username': username,
            'Input.Password': password,
            'Input.button': 'login',
            '__RequestVerificationToken': token
        }

        # The login might result in a redirect, which requests handles automatically
        urls_called.append(f"POST {login_url}")
        post_response = req_session.post(login_url, data=login_data)
        post_response.raise_for_status()

        # Step 3: Fetch the Event List
        event_list_url = f"https://coordinator.iamresponding.com/api/EventList?days={days}"
        urls_called.append(f"GET {event_list_url}")
        event_list_response = req_session.get(event_list_url)
        
        if event_list_response.status_code != 200:
            logger.error(f"Scrape failed: Event list status code {event_list_response.status_code}", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
            return jsonify({'error': 'Failed to fetch event list. Invalid credentials or API changed.', 'status_code': event_list_response.status_code}), 401

        try:
            events = event_list_response.json()
        except Exception as e:
             logger.error("Scrape failed: Failed to parse event list JSON", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
             return jsonify({'error': 'Failed to parse event list JSON. Are credentials correct?', 'details': str(e)}), 401

        # Step 4: Fetch details for each event
        current_date_str = datetime.datetime.now().strftime('%Y-%m-%d')
        
        detailed_events = []
        
        for event in events:
            event_id = event.get('id')
            if not event_id:
                continue
                
            detail_url = f"https://coordinator.iamresponding.com/api/EventDetail?eventID={event_id}&recurrenceStartDate={current_date_str}T15:30:00"
            urls_called.append(f"GET {detail_url}")
            detail_response = req_session.get(detail_url)
            
            if detail_response.status_code == 200:
                try:
                    detail_data = detail_response.json()
                    
                    # Extract the required fields
                    subject = detail_data.get('subject', '')
                    event_start = detail_data.get('eventStart', '')
                    event_end = detail_data.get('eventEnd', '')
                    description = detail_data.get('description', '')
                    
                    # Filter attendees where response == 1 (attending)
                    all_attendees = detail_data.get('eventAttendees', [])
                    attending = [a for a in all_attendees if a.get('response') == 1]
                    
                    detailed_events.append({
                        'id': event_id,
                        'subject': subject,
                        'eventStart': event_start,
                        'eventEnd': event_end,
                        'description': description,
                        'attendees': attending
                    })
                    
                    # Business Data Logging
                    logger.info(
                        f"Fetched Event: {subject} ({len(attending)} attending)",
                        extra={"tags": {
                            "event_type": "business_data",
                            "action": "event_fetched",
                            "iam_event_id": str(event_id),
                            "iam_subject": subject,
                            "iam_attendees_count": str(len(attending))
                        }}
                    )
                except Exception as e:
                    # Skip if JSON parsing fails for a specific event
                    continue

        # Sort events by start date if it exists
        detailed_events.sort(key=lambda x: x.get('eventStart', ''))

        scrape_duration = time.time() - scrape_start_time
        logger.info(f"Scrape completed successfully in {scrape_duration:.2f}s. Fetched {len(detailed_events)} events.", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_completed", "duration_seconds": str(round(scrape_duration, 2)), "events_count": str(len(detailed_events))}})

        return jsonify({'events': detailed_events, 'urls': urls_called})

    except requests.exceptions.RequestException as e:
        logger.error(f"Scrape failed: Network request error - {str(e)}", extra={"tags": {"event_type": "app_telemetry", "action": "scrape_error"}})
        return jsonify({'error': 'Network request failed', 'details': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)

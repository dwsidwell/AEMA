import os
import requests
from bs4 import BeautifulSoup
import datetime
from zoneinfo import ZoneInfo
import logging
import logging_loki
from dotenv import load_dotenv
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

# Load environment variables
load_dotenv()

# Configuration
AGENCY = os.environ.get('IAM_AGENCY')
USERNAME = os.environ.get('IAM_USERNAME')
PASSWORD = os.environ.get('IAM_PASSWORD')

SMTP_SERVER = os.environ.get('SMTP_SERVER', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', 587))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
EMAIL_SENDER = os.environ.get('EMAIL_SENDER', SMTP_USER)

# Hardcoded CCs for testing
CC_EMAILS = ["dwsidwell@gmail.com"]
# Hardcoded testing filter
TESTING_LAST_NAMES = ["Sidwell V31", "Schur V22"]

DAYS_TO_PULL = 7

# Grafana Loki Setup
logger = logging.getLogger("email_attendees")
logger.setLevel(logging.INFO)
loki_url = os.environ.get('GRAFANA_LOKI_URL')
loki_username = os.environ.get('GRAFANA_LOKI_USERNAME')
loki_password = os.environ.get('GRAFANA_LOKI_PASSWORD')

if loki_url and loki_username:
    handler = logging_loki.LokiHandler(
        url=loki_url,
        tags={"application": "iamresponding-viewer", "script": "email_attendee_reminders"},
        auth=(loki_username, loki_password),
        version="1",
    )
    logger.addHandler(handler)
else:
    logging.basicConfig(level=logging.INFO)
    logger.warning("Grafana Loki credentials not fully configured. Logging to console only.")

def fetch_events():
    if not all([AGENCY, USERNAME, PASSWORD]):
        logger.error("Scrape failed: Missing IamResponding credentials in .env", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
        return None

    logger.info(f"Starting scrape for agency: {AGENCY}, days: {DAYS_TO_PULL}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_started"}})
    req_session = requests.Session()
    req_session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
    })

    try:
        # Step 1: Login Page
        login_url = "https://auth.iamresponding.com/login/member"
        get_response = req_session.get(login_url)
        get_response.raise_for_status()

        soup = BeautifulSoup(get_response.text, 'html.parser')
        token_input = soup.find('input', {'name': '__RequestVerificationToken'})
        
        if not token_input:
            logger.error("Scrape failed: Could not find RequestVerificationToken", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
            return None
        
        token = token_input.get('value')

        # Step 2: Post Login
        login_data = {
            'Input.Agency': AGENCY,
            'Input.Username': USERNAME,
            'Input.Password': PASSWORD,
            'Input.button': 'login',
            '__RequestVerificationToken': token
        }

        post_response = req_session.post(login_url, data=login_data)
        post_response.raise_for_status()

        # Step 3: Fetch Event List
        event_list_url = f"https://coordinator.iamresponding.com/api/EventList?days={DAYS_TO_PULL}"
        event_list_response = req_session.get(event_list_url)
        
        if event_list_response.status_code != 200:
            logger.error(f"Scrape failed: Event list status code {event_list_response.status_code}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
            return None

        events = event_list_response.json()

        # Step 4: Fetch details
        detailed_events = []
        
        for event in events:
            event_id = event.get('id')
            event_start_str = event.get('eventStart', '')
            
            try:
                dt_utc = datetime.datetime.strptime(event_start_str[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
                dt_central = dt_utc.astimezone(ZoneInfo("America/Chicago"))
                central_start_str = dt_central.strftime('%Y-%m-%dT%H:%M:%S')
            except Exception:
                central_start_str = event_start_str

            if not event_id:
                continue
                
            detail_url = f"https://coordinator.iamresponding.com/api/EventDetail?eventID={event_id}&recurrenceStartDate={central_start_str}"
            detail_response = req_session.get(detail_url)
            
            if detail_response.status_code == 200:
                try:
                    detail_data = detail_response.json()
                    
                    subject = detail_data.get('subject', '')
                    event_start = detail_data.get('eventStart', '')
                    event_end = detail_data.get('eventEnd', '')
                    description = detail_data.get('description', '')
                    
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
                except Exception as e:
                    continue

        detailed_events.sort(key=lambda x: x.get('eventStart', ''))
        
        logger.info(f"Scrape completed successfully. Fetched {len(detailed_events)} events.", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_completed", "events_count": str(len(detailed_events))}})
        return detailed_events

    except Exception as e:
        logger.error(f"Scrape failed: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
        return None

def group_events_by_attendee(events):
    grouped = {}
    for event in events:
        for a in event.get('attendees', []):
            member = a.get('member', {})
            member_email = member.get('memberEmail')
            member_id = member.get('memberId')
            
            if not member_email:
                continue
                
            if member_id not in grouped:
                grouped[member_id] = {
                    'member': member,
                    'events': []
                }
            grouped[member_id]['events'].append(event)
    return grouped

def format_html_email(member, events):
    first_name = member.get('name', '')
    
    html = f"""
    <html>
    <head>
    <style>
        body {{ font-family: Arial, sans-serif; color: #333; }}
        h2 {{ color: #2c3e50; }}
        table {{ border-collapse: collapse; width: 100%; max-width: 1000px; margin-bottom: 20px; }}
        th, td {{ border: 1px solid #dddddd; text-align: left; padding: 10px; }}
        th {{ background-color: #f8f9fa; color: #495057; }}
    </style>
    </head>
    <body>
    <h2>Hi {first_name},</h2>
    <p>Here are the upcoming IamResponding EMA events you are signed up for in the next 7 days:</p>
    <table>
        <tr>
            <th style="width: 25%;">Date & Time</th>
            <th style="width: 30%;">Subject</th>
            <th style="width: 45%;">Description</th>
        </tr>
    """

    for event in events:
        start_time = event.get('eventStart', '')
        try:
            # Parse as UTC, then convert to Central Time
            dt_utc = datetime.datetime.strptime(start_time[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
            dt_central = dt_utc.astimezone(ZoneInfo("America/Chicago"))
            formatted_time = dt_central.strftime('%m/%d/%Y %I:%M %p')
        except ValueError:
            formatted_time = start_time.replace('T', ' ')
        
        html += f"""
        <tr>
            <td style="white-space: nowrap;">{formatted_time}</td>
            <td>{event.get('subject', '')}</td>
            <td>{event.get('description', '')}</td>
        </tr>
        """

    html += """
    </table>
    <p style="font-size: 12px; color: #7f8c8d;">This is an automated reminder.</p>
    </body>
    </html>
    """
    return html

def send_email(to_email, html_content):
    if not all([SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_SENDER]):
        logger.error("Email configuration is missing or incomplete in .env", extra={"tags": {"event_type": "script_telemetry", "action": "email_error"}})
        return False

    msg = MIMEMultipart('alternative')
    msg['Subject'] = "TEST: EMA events you are signed up for"
    msg['From'] = EMAIL_SENDER
    msg['To'] = to_email
    msg['Cc'] = ", ".join(CC_EMAILS)
    msg['Date'] = formatdate(localtime=True)

    part = MIMEText(html_content, 'html')
    msg.attach(part)
    
    recipients = [to_email] + CC_EMAILS

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        server.quit()
        logger.info(f"Email sent successfully to {to_email} (CC: {', '.join(CC_EMAILS)}).", extra={"tags": {"event_type": "script_telemetry", "action": "email_sent"}})
        return True
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "email_error"}})
        return False

if __name__ == "__main__":
    logger.info("Starting IamResponding Attendee Email Script")
    events = fetch_events()
    
    if events:
        attendee_groups = group_events_by_attendee(events)
        sent_count = 0
        skipped_count = 0
        
        for member_id, data in attendee_groups.items():
            member = data['member']
            member_events = data['events']
            last_name = member.get('lastName', '')
            member_email = member.get('memberEmail')
            
            # --- TESTING GUARD ---
            # Remove or comment out this block when moving to production
            if last_name not in TESTING_LAST_NAMES:
                skipped_count += 1
                continue
            
            html_content = format_html_email(member, member_events)
            success = send_email(member_email, html_content)
            if success:
                sent_count += 1
                
        logger.info(f"Finished processing attendees. Sent: {sent_count}, Skipped (due to testing filter): {skipped_count}")
    else:
        logger.warning("No events returned or scrape failed.")
        
    logger.info("Script execution finished")

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
from email.mime.image import MIMEImage
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
# Filter out empty strings in case the env var has trailing commas or spaces
EMAIL_RECIPIENTS = [email.strip() for email in os.environ.get('EMAIL_RECIPIENTS', '').split(',') if email.strip()]

DAYS_TO_PULL = 15

# Grafana Loki Setup
logger = logging.getLogger("email_events")
logger.setLevel(logging.INFO)
loki_url = os.environ.get('GRAFANA_LOKI_URL')
loki_username = os.environ.get('GRAFANA_LOKI_USERNAME')
loki_password = os.environ.get('GRAFANA_LOKI_PASSWORD')

if loki_url and loki_username:
    handler = logging_loki.LokiHandler(
        url=loki_url,
        tags={"application": "iamresponding-viewer", "script": "email_events"},
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
                        'attendees': attending,
                        'attendee_count': len(attending)
                    })
                except Exception as e:
                    continue

        detailed_events.sort(key=lambda x: x.get('eventStart', ''))
        
        logger.info(f"Scrape completed successfully. Fetched {len(detailed_events)} events.", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_completed", "events_count": str(len(detailed_events))}})
        return detailed_events

    except Exception as e:
        logger.error(f"Scrape failed: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
        return None

def format_html_email(events):
    if not events:
        return "<p>No upcoming events found for the next 15 days.</p>"

    html = """
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <style>
        body { font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f7f6; color: #333333; margin: 0; padding: 20px; }
        .wrapper { max-width: 800px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }
        .header { background-color: #1e3a8a; color: #ffffff; padding: 30px; text-align: center; }
        .header img { max-height: 80px; margin-bottom: 15px; border-radius: 4px; }
        .header h2 { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }
        .content { padding: 30px; }
        .table-wrapper { border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; }
        table { width: 100%; border-collapse: collapse; background-color: #ffffff; }
        th { background-color: #f8fafc; color: #475569; font-weight: 600; padding: 15px; text-align: left; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; border: 1px solid #cbd5e1; border-bottom: 2px solid #94a3b8; }
        td { padding: 15px; border: 1px solid #cbd5e1; vertical-align: top; font-size: 15px; line-height: 1.5; }
        .attendee-list { margin: 0; padding-left: 20px; color: #334155; }
        .date-badge { background-color: #eff6ff; color: #1e40af; padding: 6px 10px; border-radius: 6px; font-weight: 600; display: inline-block; font-size: 13px; white-space: nowrap; margin-bottom: 5px; }
        .subject { font-weight: 600; color: #0f172a; margin-bottom: 5px; font-size: 16px; }
        .desc { color: #64748b; font-size: 14px; }
        .footer { background-color: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }
    </style>
    </head>
    <body>
    <div class="wrapper">
        <div class="header">
            <img src="cid:aema_logo" alt="AEMA Logo">
            <h2>Upcoming Volunteer Events</h2>
            <p style="margin: 10px 0 0 0; color: #bfdbfe; font-size: 14px;">Next 15 Days Overview</p>
        </div>
        <div class="content">
            <div class="table-wrapper">
                <table>
                    <tr>
                        <th style="width: 20%;">Date & Time</th>
                        <th style="width: 45%;">Event Details</th>
                        <th style="width: 35%;">Attendees</th>
                    </tr>
    """

    for i, event in enumerate(events):
        start_time = event.get('eventStart', '')
        try:
            # Parse as UTC, then convert to Central Time
            dt_utc = datetime.datetime.strptime(start_time[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
            dt_central = dt_utc.astimezone(ZoneInfo("America/Chicago"))
            formatted_time = dt_central.strftime('%m/%d/%Y %I:%M %p')
            date_part, time_part = formatted_time.split(' ', 1)
        except ValueError:
            date_part = start_time[:10]
            time_part = start_time[11:16]

        attendees = event.get('attendees', [])
        attendee_count = len(attendees)
        
        if attendee_count > 0:
            attendee_list_items = "".join([f"<li>{a.get('member', {}).get('name', '').strip()} {a.get('member', {}).get('lastName', '').strip()}</li>" for a in attendees])
            attendees_display = f"<ul class='attendee-list'>{attendee_list_items}</ul>"
            td_style = "background-color: #ffffff;"
        else:
            attendees_display = "<em>None</em>"
            td_style = "background-color: #fee2e2; color: #991b1b; border-color: #fca5a5;"
        
        desc = event.get('description', '')
        words = desc.split()
        if len(words) > 50:
            desc = " ".join(words[:50]) + "..."

        html += f"""
                    <tr>
                        <td style="{td_style} border-left: { '4px solid #ef4444' if attendee_count == 0 else '' };">
                            <div class="date-badge">{date_part}</div>
                            <div style="font-size: 14px; color: #64748b; margin-top: 4px; font-weight: 500;">{time_part}</div>
                        </td>
                        <td style="{td_style}">
                            <div class="subject">{event.get('subject', '')}</div>
                            <div class="desc">{desc}</div>
                        </td>
                        <td style="{td_style}">{attendees_display}</td>
                    </tr>
        """

    html += """
                </table>
            </div>
        </div>
        <div class="footer">
            This email was generated automatically by the IamResponding Event Viewer.
        </div>
    </div>
    </body>
    </html>
    """
    return html

def send_email(html_content):
    if not all([SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_SENDER, EMAIL_RECIPIENTS]):
        logger.error("Email configuration is missing or incomplete in .env", extra={"tags": {"event_type": "script_telemetry", "action": "email_error"}})
        return False

    msg = MIMEMultipart('related')
    msg['Subject'] = f"Upcoming EMA volunteer events and current attendance - {datetime.date.today().strftime('%m/%d/%Y')}"
    msg['From'] = EMAIL_SENDER
    msg['To'] = ", ".join(EMAIL_RECIPIENTS)
    msg['Date'] = formatdate(localtime=True)

    msg_alternative = MIMEMultipart('alternative')
    msg.attach(msg_alternative)

    part = MIMEText(html_content, 'html')
    msg_alternative.attach(part)

    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'AEMA_Logo.jpg')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            msg_image = MIMEImage(f.read())
            msg_image.add_header('Content-ID', '<aema_logo>')
            msg_image.add_header('Content-Disposition', 'inline', filename='AEMA_Logo.jpg')
            msg.attach(msg_image)
    else:
        logger.warning("Logo image not found at %s. Sending without logo.", logo_path)

    try:
        logger.info(f"Connecting to SMTP server {SMTP_SERVER}:{SMTP_PORT}")
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECIPIENTS, msg.as_string())
        server.quit()
        logger.info(f"Email sent successfully to {len(EMAIL_RECIPIENTS)} recipient(s).", extra={"tags": {"event_type": "script_telemetry", "action": "email_sent"}})
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "email_error"}})
        return False

if __name__ == "__main__":
    logger.info("Starting IamResponding Event Email Script")
    events = fetch_events()
    if events is not None:
        html_content = format_html_email(events)
        send_email(html_content)
    else:
        logger.warning("No events returned or scrape failed. Email will not be sent.")
    logger.info("Script execution finished")

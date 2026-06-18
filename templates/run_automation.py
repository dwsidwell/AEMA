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
import json
import urllib.parse

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
_cached_automation_data = None
_attempted_init = False

def get_cached_data(key):
    global _cached_automation_data, _attempted_init
    internal_web_url = os.environ.get('INTERNAL_WEB_URL')
    if internal_web_url and not _attempted_init:
        _attempted_init = True
        secret_key = os.environ.get('SECRET_KEY')
        if not secret_key:
            secret_key = os.environ.get('SITE_PASSWORD')
        try:
            url = f"{internal_web_url.rstrip('/')}/api/private/automation-data"
            headers = {'X-Internal-Token': secret_key or ''}
            logger.info(f"Fetching automation data from internal API: {url}")
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                _cached_automation_data = resp.json()
                logger.info("Successfully fetched automation data from internal API.")
            else:
                logger.error(f"Failed to fetch automation data from internal API: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Error fetching automation data from internal API: {e}")

    if _cached_automation_data and key in _cached_automation_data:
        return _cached_automation_data[key]
    return None

def load_settings():
    cached = get_cached_data('settings')
    if cached is not None:
        return cached
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading settings.json in automation script: {e}")
    return {}

def get_weekly_summary_recipients():
    settings = load_settings()
    emails_str = settings.get('weekly_summary_to_emails', '')
    if emails_str:
        return [email.strip() for email in emails_str.split(',') if email.strip()]
    # Fallback to env variable
    return [email.strip() for email in os.environ.get('EMAIL_RECIPIENTS', '').split(',') if email.strip()]

def get_attendee_reminder_cc():
    settings = load_settings()
    emails_str = settings.get('attendee_reminder_cc_emails', '')
    if emails_str is not None and emails_str.strip() != '':
        return [email.strip() for email in emails_str.split(',') if email.strip()]
    # Fallback to default
    return ["dwsidwell@gmail.com"]

# Fetch 15 days of data for the summary email
DAYS_TO_PULL_SUMMARY = 15
# Reminders will look at the next 10 days
DAYS_TO_PULL_REMINDERS = 10

# Grafana Loki Setup
logger = logging.getLogger("run_automation")
logger.setLevel(logging.INFO)
loki_url = os.environ.get('GRAFANA_LOKI_URL')
loki_username = os.environ.get('GRAFANA_LOKI_USERNAME')
loki_password = os.environ.get('GRAFANA_LOKI_PASSWORD')

if loki_url and loki_username:
    handler = logging_loki.LokiHandler(
        url=loki_url,
        tags={"application": "iamresponding-viewer", "script": "run_automation"},
        auth=(loki_username, loki_password),
        version="1",
    )
    logger.addHandler(handler)
else:
    logging.basicConfig(level=logging.INFO)
    logger.warning("Grafana Loki credentials not fully configured. Logging to console only.")

DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
EVENT_DOCS_FILE = os.path.join(DATA_DIR, 'event_documents.json')
EVENT_LINKS_FILE = os.path.join(DATA_DIR, 'event_links.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
PORTAL_URL = os.environ.get('PORTAL_URL', 'http://127.0.0.1:5000').rstrip('/')

def load_event_documents():
    cached = get_cached_data('event_documents')
    if cached is not None:
        return cached
    if not os.path.exists(EVENT_DOCS_FILE):
        return {}
    try:
        with open(EVENT_DOCS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading event documents file: {e}")
    return {}

def load_event_links():
    cached = get_cached_data('event_links')
    if cached is not None:
        return cached
    if not os.path.exists(EVENT_LINKS_FILE):
        return {}
    try:
        with open(EVENT_LINKS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading event links file: {e}")
    return {}

def fetch_events(days):
    if not all([AGENCY, USERNAME, PASSWORD]):
        logger.error("Scrape failed: Missing IamResponding credentials in .env", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
        return None

    logger.info(f"Starting scrape for agency: {AGENCY}, days: {days}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_started"}})
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
        event_list_url = f"https://coordinator.iamresponding.com/api/EventList?days={days}"
        event_list_response = req_session.get(event_list_url)
        
        if event_list_response.status_code != 200:
            logger.error(f"Scrape failed: Event list status code {event_list_response.status_code}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
            return None

        events = event_list_response.json()

        # Step 4: Fetch details
        detailed_events = []
        event_docs = load_event_documents()
        event_links = load_event_links()
        
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
                        'attendee_count': len(attending),
                        'documents': event_docs.get(str(event_id), []),
                        'links': event_links.get(str(event_id), [])
                    })
                except Exception as e:
                    continue

        detailed_events.sort(key=lambda x: x.get('eventStart', ''))
        
        logger.info(f"Scrape completed successfully. Fetched {len(detailed_events)} events.", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_completed", "events_count": str(len(detailed_events))}})
        return detailed_events

    except Exception as e:
        logger.error(f"Scrape failed: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "scrape_error"}})
        return None

def group_events_by_attendee(events, max_days):
    grouped = {}
    
    # Filter events for max_days logic
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff_utc = now_utc + datetime.timedelta(days=max_days)
    
    filtered_events = []
    for event in events:
        start_time = event.get('eventStart', '')
        try:
            dt_utc = datetime.datetime.strptime(start_time[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
            if dt_utc <= cutoff_utc:
                filtered_events.append(event)
        except ValueError:
            # If we can't parse it, include it just to be safe
            filtered_events.append(event)

    for event in filtered_events:
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

def format_events_summary(events):
    if not events:
        return "<p>No upcoming events found.</p>"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f7f6; color: #333333; margin: 0; padding: 20px; }}
        .wrapper {{ max-width: 800px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }}
        .header {{ background-color: #1e3a8a; color: #ffffff; padding: 30px; text-align: center; }}
        .header img {{ max-height: 80px; margin-bottom: 15px; border-radius: 4px; }}
        .header h2 {{ margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }}
        .content {{ padding: 30px; }}
        .table-wrapper {{ border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; }}
        table {{ width: 100%; border-collapse: collapse; background-color: #ffffff; }}
        th {{ background-color: #f8fafc; color: #475569; font-weight: 600; padding: 15px; text-align: left; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; border: 1px solid #cbd5e1; border-bottom: 2px solid #94a3b8; }}
        td {{ padding: 15px; border: 1px solid #cbd5e1; vertical-align: top; font-size: 15px; line-height: 1.5; }}
        .attendee-list {{ margin: 0; padding-left: 20px; color: #334155; }}
        .date-badge {{ background-color: #eff6ff; color: #1e40af; padding: 6px 10px; border-radius: 6px; font-weight: 600; display: inline-block; font-size: 13px; white-space: nowrap; margin-bottom: 5px; }}
        .subject {{ font-weight: 600; color: #0f172a; margin-bottom: 5px; font-size: 16px; }}
        .desc {{ color: #64748b; font-size: 14px; }}
        .footer {{ background-color: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
    </style>
    </head>
    <body>
    <div class="wrapper">
        <div class="header">
            <img src="cid:aema_logo" alt="AEMA Logo" height="80">
            <h2>Upcoming Volunteer Events</h2>
            <p style="margin: 10px 0 0 0; color: #bfdbfe; font-size: 14px;">Next {DAYS_TO_PULL_SUMMARY} Days Overview</p>
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

        attachments_html = ""
        documents = event.get('documents', [])
        links = event.get('links', [])
        if documents or links:
            attachments_html += '<div style="margin-top: 10px; font-family: sans-serif;">'
            for doc in documents:
                file_url = f"{PORTAL_URL}/api/event-document/{event.get('id')}/{urllib.parse.quote(doc['filename'])}"
                attachments_html += f"""
                <a href="{file_url}" target="_blank" style="display: inline-block; background-color: #ecfeff; border: 1px solid #a5f3fc; color: #0891b2; padding: 4px 8px; border-radius: 4px; font-size: 12px; text-decoration: none; margin: 2px 4px 2px 0; font-weight: 500;">
                    📄 {doc['filename']} ({doc['file_size']})
                </a>
                """
            for link in links:
                attachments_html += f"""
                <a href="{link['url']}" target="_blank" style="display: inline-block; background-color: #faf5ff; border: 1px solid #e9d5ff; color: #7c3aed; padding: 4px 8px; border-radius: 4px; font-size: 12px; text-decoration: none; margin: 2px 4px 2px 0; font-weight: 500;">
                    🔗 {link['description']}
                </a>
                """
            attachments_html += '</div>'

        html += f"""
                    <tr>
                        <td style="{td_style} border-left: { '4px solid #ef4444' if attendee_count == 0 else '' };">
                            <div class="date-badge">{date_part}</div>
                            <div style="font-size: 14px; color: #64748b; margin-top: 4px; font-weight: 500;">{time_part}</div>
                        </td>
                        <td style="{td_style}">
                            <div class="subject">{event.get('subject', '')}</div>
                            <div class="desc">{desc}</div>
                            {attachments_html}
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


def format_attendee_reminder(member, events):
    first_name = member.get('name', '')
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f7f6; color: #333333; margin: 0; padding: 20px; }}
        .wrapper {{ max-width: 800px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 15px rgba(0,0,0,0.05); }}
        .header {{ background-color: #1e3a8a; color: #ffffff; padding: 30px; text-align: center; }}
        .header img {{ max-height: 80px; margin-bottom: 15px; border-radius: 4px; }}
        .header h2 {{ margin: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }}
        .content {{ padding: 30px; }}
        .table-wrapper {{ border-radius: 8px; overflow: hidden; border: 1px solid #e2e8f0; }}
        table {{ width: 100%; border-collapse: collapse; background-color: #ffffff; }}
        th {{ background-color: #f8fafc; color: #475569; font-weight: 600; padding: 15px; text-align: left; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; border: 1px solid #cbd5e1; border-bottom: 2px solid #94a3b8; }}
        td {{ padding: 15px; border: 1px solid #cbd5e1; vertical-align: top; font-size: 15px; line-height: 1.5; }}
        .date-badge {{ background-color: #eff6ff; color: #1e40af; padding: 6px 10px; border-radius: 6px; font-weight: 600; display: inline-block; font-size: 13px; white-space: nowrap; margin-bottom: 5px; }}
        .subject {{ font-weight: 600; color: #0f172a; margin-bottom: 5px; font-size: 16px; }}
        .desc {{ color: #64748b; font-size: 14px; }}
        .footer {{ background-color: #f8fafc; padding: 20px; text-align: center; font-size: 12px; color: #94a3b8; border-top: 1px solid #e2e8f0; }}
    </style>
    </head>
    <body>
    <div class="wrapper">
        <div class="header">
            <img src="cid:aema_logo" alt="AEMA Logo" height="80">
            <h2>Upcoming Volunteer Events</h2>
            <p style="margin: 10px 0 0 0; color: #bfdbfe; font-size: 14px;">Next {DAYS_TO_PULL_REMINDERS} Days Overview</p>
        </div>
        <div class="content">
    """
    
    if not events:
        html += f"""
            <p>Hi {first_name},</p>
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 30px; text-align: center; color: #64748b; font-size: 16px; margin: 10px 0;">
                You are not signed up for any events in this time period.
            </div>
        """
    else:
        html += f"""
            <p>Hi {first_name},</p>
            <p>Here are the upcoming IamResponding EMA events you are signed up for in the next {DAYS_TO_PULL_REMINDERS} days:</p>
            <div class="table-wrapper">
                <table>
                    <tr>
                        <th style="width: 20%;">Date & Time</th>
                        <th style="width: 50%;">Event Details</th>
                        <th style="width: 30%;">You are volunteering with</th>
                    </tr>
        """
        for event in events:
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
            
            desc = event.get('description', '')
            words = desc.split()
            if len(words) > 50:
                desc = " ".join(words[:50]) + "..."
                
            td_style = "background-color: #ffffff;"
            
            attachments_html = ""
            documents = event.get('documents', [])
            links = event.get('links', [])
            if documents or links:
                attachments_html += '<div style="margin-top: 10px; font-family: sans-serif;">'
                for doc in documents:
                    file_url = f"{PORTAL_URL}/api/event-document/{event.get('id')}/{urllib.parse.quote(doc['filename'])}"
                    attachments_html += f"""
                    <a href="{file_url}" target="_blank" style="display: inline-block; background-color: #ecfeff; border: 1px solid #a5f3fc; color: #0891b2; padding: 4px 8px; border-radius: 4px; font-size: 12px; text-decoration: none; margin: 2px 4px 2px 0; font-weight: 500;">
                        📄 {doc['filename']} ({doc['file_size']})
                    </a>
                    """
                for link in links:
                    attachments_html += f"""
                    <a href="{link['url']}" target="_blank" style="display: inline-block; background-color: #faf5ff; border: 1px solid #e9d5ff; color: #7c3aed; padding: 4px 8px; border-radius: 4px; font-size: 12px; text-decoration: none; margin: 2px 4px 2px 0; font-weight: 500;">
                        🔗 {link['description']}
                    </a>
                    """
                attachments_html += '</div>'

            # Get other attendees
            current_member_id = str(member.get('memberId', ''))
            current_email = member.get('memberEmail', '').strip().lower()
            current_name = f"{member.get('name', '').strip().lower()} {member.get('lastName', '').strip().lower()}"

            other_attendees = []
            for a in event.get('attendees', []):
                m = a.get('member', {})
                m_id = str(m.get('memberId', ''))
                m_email = m.get('memberEmail', '').strip().lower()
                m_name = f"{m.get('name', '').strip().lower()} {m.get('lastName', '').strip().lower()}"
                
                is_current = False
                if current_member_id and m_id == current_member_id:
                    is_current = True
                elif current_email and m_email == current_email:
                    is_current = True
                elif m_name == current_name:
                    is_current = True
                    
                if not is_current:
                    other_attendees.append(f"{m.get('name', '').strip()} {m.get('lastName', '').strip()}")

            if not other_attendees:
                other_display = "<em>None</em>"
            else:
                other_list_items = "".join([f"<li style='margin-bottom: 4px;'>{name}</li>" for name in other_attendees])
                other_display = f"<ul style='margin: 0; padding-left: 20px; color: #334155;'>{other_list_items}</ul>"

            html += f"""
                        <tr>
                            <td style="{td_style}">
                                <div class="date-badge">{date_part}</div>
                                <div style="font-size: 14px; color: #64748b; margin-top: 4px; font-weight: 500;">{time_part}</div>
                            </td>
                            <td style="{td_style}">
                                <div class="subject">{event.get('subject', '')}</div>
                                <div class="desc">{desc}</div>
                                {attachments_html}
                            </td>
                            <td style="{td_style}">
                                {other_display}
                            </td>
                        </tr>
            """
        html += """
                </table>
            </div>
        """
        
    html += """
        </div>
        <div class="footer">
            This is an automated reminder from the IamResponding Event Viewer.
        </div>
    </div>
    </body>
    </html>
    """
    return html

def attach_logo(msg):
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'AEMA_Logo.jpg')
    if os.path.exists(logo_path):
        with open(logo_path, 'rb') as f:
            msg_image = MIMEImage(f.read())
            msg_image.add_header('Content-ID', '<aema_logo>')
            msg_image.add_header('Content-Disposition', 'inline', filename='AEMA_Logo.jpg')
            msg.attach(msg_image)
    else:
        logger.warning("Logo image not found at %s. Sending without logo.", logo_path)


def send_summary_email(html_content, server):
    recipients = get_weekly_summary_recipients()
    if not recipients:
        return False
        
    msg = MIMEMultipart('related')
    msg['Subject'] = f"Upcoming EMA volunteer events and current attendance - {datetime.date.today().strftime('%m/%d/%Y')}"
    msg['From'] = EMAIL_SENDER
    msg['To'] = ", ".join(recipients)
    msg['Date'] = formatdate(localtime=True)

    msg_alternative = MIMEMultipart('alternative')
    msg.attach(msg_alternative)

    part = MIMEText(html_content, 'html')
    msg_alternative.attach(part)
    
    attach_logo(msg)

    try:
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        logger.info(f"Summary email sent successfully to {len(recipients)} recipient(s).", extra={"tags": {"event_type": "script_telemetry", "action": "summary_email_sent"}})
        return True
    except Exception as e:
        logger.error(f"Failed to send summary email: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "summary_email_error"}})
        return False

def send_attendee_email(to_email, html_content, server):
    cc_emails = get_attendee_reminder_cc()
    msg = MIMEMultipart('related')
    msg['Subject'] = "Upcoming AEMA events you are signed up for"
    msg['From'] = EMAIL_SENDER
    msg['To'] = to_email
    if cc_emails:
        msg['Cc'] = ", ".join(cc_emails)
    msg['Date'] = formatdate(localtime=True)

    msg_alternative = MIMEMultipart('alternative')
    msg.attach(msg_alternative)

    part = MIMEText(html_content, 'html')
    msg_alternative.attach(part)
    
    attach_logo(msg)
    
    recipients = [to_email] + cc_emails

    try:
        server.sendmail(EMAIL_SENDER, recipients, msg.as_string())
        logger.info(f"Attendee email sent successfully to {to_email} (CC: {', '.join(cc_emails)}).", extra={"tags": {"event_type": "script_telemetry", "action": "attendee_email_sent"}})
        return True
    except Exception as e:
        logger.error(f"Failed to send attendee email to {to_email}: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "attendee_email_error"}})
        return False



def load_users():
    cached = get_cached_data('users')
    if cached is not None:
        return cached
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading users.json in automation script: {e}")
    return {}

def find_user_in_registry(member, users):
    member_id = str(member.get('memberId', ''))
    member_email = member.get('memberEmail', '').strip().lower()
    first_name = member.get('name', '').strip().lower()
    last_name = member.get('lastName', '').strip().lower()
    
    # Try matching by member ID first
    for user in users.values():
        iar_id = str(user.get('IAR_memberId', ''))
        if iar_id and iar_id == member_id:
            return user
            
    # Try matching by email
    if member_email:
        for user in users.values():
            user_email = user.get('email', '').strip().lower()
            if user_email and user_email == member_email:
                return user
                
    # Try matching by first and last name
    for user in users.values():
        u_first = user.get('first_name', '').strip().lower()
        u_last = user.get('last_name', '').strip().lower()
        iar_first = user.get('iar_first_name', '')
        iar_last = user.get('iar_last_name', '')
        u_iar_first = iar_first.strip().lower() if iar_first else u_first
        u_iar_last = iar_last.strip().lower() if iar_last else u_last
        
        if (u_first == first_name or u_iar_first == first_name) and (u_last == last_name or u_iar_last == last_name):
            return user
            
    return None

if __name__ == "__main__":
    logger.info("Starting IamResponding Unified Email Script")
    
    if not all([SMTP_SERVER, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_SENDER]):
        logger.error("Email configuration is missing or incomplete in .env. Exiting.")
        exit(1)
        
    events = fetch_events(DAYS_TO_PULL_SUMMARY)
    
    if events:
        try:
            logger.info(f"Connecting to SMTP server {SMTP_SERVER}:{SMTP_PORT}")
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            
            # 1. Send Main Summary Email (15 days)
            summary_html = format_events_summary(events)
            send_summary_email(summary_html, server)
            
            # 2. Send Attendee Reminders (Filtered to 10 days)
            users_registry = load_users()
            attendee_groups = group_events_by_attendee(events, DAYS_TO_PULL_REMINDERS)
            sent_count = 0
            skipped_count = 0
            
            for user_key, user_profile in users_registry.items():
                first_name = user_profile.get('first_name', '')
                last_name = user_profile.get('last_name', '')
                
                # Check weekly reminder preference (must exist and be explicitly enabled)
                if not user_profile.get('weekly_reminder_email', False):
                    logger.info(f"Skipping attendee reminder for {first_name} {last_name}: weekly reminder email is not enabled (set to No) in profile.")
                    skipped_count += 1
                    continue
                
                # Check if this user is active
                if not user_profile.get('is_active', True):
                    continue

                # Find if this user is in attendee_groups
                member_events = []
                member_iar = {
                    'name': user_profile.get('iar_first_name') or first_name,
                    'lastName': user_profile.get('iar_last_name') or last_name,
                    'memberEmail': user_profile.get('email', ''),
                    'memberId': user_profile.get('IAR_memberId', '')
                }
                
                # Try to find user in attendee_groups using find_user_in_registry matching logic
                matched_data = None
                for member_id, data in attendee_groups.items():
                    m = data['member']
                    matched_profile = find_user_in_registry(m, {user_key: user_profile})
                    if matched_profile:
                        matched_data = data
                        break
                
                if matched_data:
                    member_events = matched_data['events']
                    member_iar = matched_data['member'] # Use scraped member info if matched
                
                # Send email (either with events list or empty notice)
                html_content = format_attendee_reminder(member_iar, member_events)
                to_email = member_iar.get('memberEmail') or user_profile.get('email')
                if not to_email:
                    logger.warning(f"Could not send reminder email to {first_name} {last_name}: No email address found.")
                    continue
                    
                success = send_attendee_email(to_email, html_content, server)
                if success:
                    sent_count += 1
                    
            logger.info(f"Finished processing attendees. Sent: {sent_count}, Skipped (opted out): {skipped_count}")
            
            server.quit()
        except Exception as e:
            logger.error(f"SMTP Connection or sending failed: {str(e)}", extra={"tags": {"event_type": "script_telemetry", "action": "smtp_error"}})
            
    else:
        logger.warning("No events returned or scrape failed. Emails will not be sent.")
        
    logger.info("Script execution finished")

import os
import pathlib
import base64
import re
import time
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
import requests
from dotenv import load_dotenv
from flask import Flask, session, abort, redirect, request
from google.oauth2 import id_token
from google_auth_oauthlib.flow import Flow
from pip._vendor import cachecontrol
import google.auth.transport.requests

load_dotenv()
app = Flask("Avo")
app.secret_key =os.getenv("CLIENT_SECRET")

os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1" # to allow Http traffic for local dev

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
client_secrets_file = os.path.join(pathlib.Path(__file__).parent, "client_secret.json")

flow = Flow.from_client_secrets_file(
    client_secrets_file=client_secrets_file,
    scopes = [
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/gmail.readonly"
    ],
    redirect_uri="http://localhost:5000/callback"
)


def credentials_dict(creds):
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": creds.scopes
    }


def login_is_required(function):
    def wrapper(*args, **kwargs):
        if "google_id" not in session:
            return abort(401)  # Authorization required
        else:
            return function()

    return wrapper


def get_body(payload):

    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8")
            if "parts" in part:
                result = get_body(part)
                if result: return result
    body_data = payload.get("body", {}).get("data", "")
    if body_data:
        return base64.urlsafe_b64decode(body_data).decode("utf-8")
    return ""


def find_email_matches(body, subject, keywords):
    if not keywords:
        return []

    pattern = re.compile(
        rf"({'|'.join(map(re.escape, keywords))})",
        re.IGNORECASE
    )

    found_keywords = set()
    for text in [subject, body]:
        found_keywords.update(m.group().lower() for m in pattern.finditer(text))
    #or maybe add checker here to validate emails before sending to search inbox
    if found_keywords:
        return {
            "keywords": list(found_keywords),
            "date": find_date(body),
            "amt": find_amount(body)
        }

    return None
    
def search_inbox(raw_messages, service):
    found_emails = []
    keywords = ["trial", "subscription", "charge", "payment due", "payment"]
    
    def batch_email(request_id, response, exception):
        if exception:
            print(f"Error: {exception}")
            return

        headers = response.get("payload", {}).get("headers", [])
        subject = next((v["value"] for v in headers if v["name"] == "Subject"), "No Subject")
        from_name = next((v["value"] for v in headers if v["name"] == "From"), "Unknown")
        
        body = get_body(response["payload"])
        
        match_data = find_email_matches(body, subject, keywords)
        # next sprint go through matched emails and double check amt + date are valid before adding

        if match_data:
            found_emails.append({
                "from": from_name,
                "subject": subject,
                "body_content": body,
                "match_details": match_data  
            })
    batch_size = 20 

    for i in range(0, len(raw_messages), batch_size):
        batch = service.new_batch_http_request(callback=batch_email)
    
        current_chunk = raw_messages[i : i + batch_size]
        for msg in current_chunk:
            batch.add(service.users().messages().get(userId="me", id=msg["id"]))
    
        batch.execute() 
        time.sleep(0.5)
    return found_emails


def find_date(text):
    date_pattern = r"""
        (?:\d{1,4}[-./]\d{1,2}[-./]\d{1,4})|                 
        (?:\d{1,2}(?:st|nd|rd|th)?\s+)?                       
        (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)   
        [a-z]*\.?                                            
        (?:\s+\d{1,2}(?:st|nd|rd|th)?)?                        
        (?:,?\s+\d{2,4})?                              
    """
    
    matches = re.findall(date_pattern, text, re.IGNORECASE | re.VERBOSE)
    if matches:
        return matches[0].strip()
    
    return "No date found."




def find_amount(text):
    patterns = [
        r'(?:Total|Plan|Amount|Due|Balance|Price|USD)\s*:?\s?\$?\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)',
        r'\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s?(?:USD)?',
        r'(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s?USD',
        r'([0-9]+\.[0-9][0-9](?:[^0-9]\b|$))'
    ]

    all_matches = []
    for p in patterns:
        matches = re.findall(p, text, re.IGNORECASE)
        for m in matches:
            val = (m.replace(',', ''))
            all_matches.append(val)

    if all_matches:
        return max(all_matches)
    
    return "Could not get amount due."
    







def render_email_html(email_data):
    details = email_data.get('match_details', {})
    
    keywords = details.get('keywords', [])
    date = details.get('date', "No date found.")
    amt = details.get('amt', "Could not get amount due.")
    
    unique_matches = ", ".join(keywords)
    count = len(keywords)


    amt_display = f"${amt:,.2f}" if isinstance(amt, (int, float)) else amt

    return (
        f"<div style='margin-bottom: 20px; border-bottom: 1px solid #ccc; padding-bottom: 10px; font-family: sans-serif;'>"
        f"  <span style='color: #006400;'><strong>Matched:</strong> {unique_matches} ({count})</span><br>"
        f"  <strong>From:</strong> {email_data['from']}<br>"
        f"  <strong>Subject:</strong> {email_data['subject']}<br>"
        f"  <span style='color: #666;'><strong>Due Date:</strong> {date}</span><br>"
        f"  <strong style='color: #d9534f;'>Amount Due:$ </strong> {amt_display}"
        f"</div>"
    )


@app.route("/protected_area")
@login_is_required
def protected_area():
    creds = Credentials(**session["credentials"])
    service = build("gmail", "v1", credentials=creds)
    
    query = "newer_than:60d"
    all_raw_messages = []
    next_page_token = None

    while True:
        results = service.users().messages().list(
            userId="me", 
            q=query, 
            maxResults=100, 
            pageToken=next_page_token
        ).execute()

        messages = results.get("messages", [])
        all_raw_messages.extend(messages)

       
        next_page_token = results.get("nextPageToken")
       
        if not next_page_token:
            break
    matched_emails = search_inbox(all_raw_messages, service)

    email_blocks = [render_email_html(e) for e in matched_emails]
    if not email_blocks:
        return "<h1>No relevant emails found.</h1>"

    return f"<h1>Found {len(email_blocks)} Results</h1>" + " ".join(email_blocks)



@app.route("/login")
def login():
    authorization_url, state = flow.authorization_url()
    session["state"] = state
    return redirect(authorization_url)


@app.route("/callback")
def callback():
    flow.fetch_token(authorization_response=request.url)

    if session.get("state") != request.args.get("state"):
        abort(500)  # State does not match!

    credentials = flow.credentials
    session["credentials"] = credentials_dict(credentials)
    request_session = requests.session()
    cached_session = cachecontrol.CacheControl(request_session)
    token_request = google.auth.transport.requests.Request(session=cached_session)

    id_info = id_token.verify_oauth2_token(
        id_token=credentials._id_token,
        request=token_request,
        audience=GOOGLE_CLIENT_ID,
        clock_skew_in_seconds = 3
    )

    session["google_id"] = id_info.get("sub")
    session["name"] = id_info.get("name")
    return redirect("/protected_area")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/")
def index():
    return "Hello World <a href='/login'><button>Login</button></a>"




if __name__ == "__main__":
    app.run(host="localhost", port=5000, debug=True)
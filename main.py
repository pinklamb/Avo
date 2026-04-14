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

    matches = []
    for text in [subject, body]:
        matches.extend([m.group().lower() for m in pattern.finditer(text)])

    return matches
    
def search_inbox(raw_messages, service):
    found_emails = []
    keywords = ["will begin","doordash","nyt"]

    def batch_email(request_id, response, exception):
        if exception:
            print(f"Error: {exception}")
            return

        headers = response.get("payload", {}).get("headers", [])
        subject = next((v["value"] for v in headers if v["name"] == "Subject"), "No Subject")
        from_name = next((v["value"] for v in headers if v["name"] == "From"), "Unknown")
        
        body = get_body(response["payload"])
        matches = find_email_matches(body, subject, keywords)

        if matches:
            found_emails.append({
                "from": from_name,
                "subject": subject,
                "body_content": body,
                "matches": matches
            })

    batch_size = 20 

    for i in range(0, len(raw_messages), batch_size):
        batch = service.new_batch_http_request(callback=batch_email)
    
        current_chunk = raw_messages[i : i + batch_size]
        for msg in current_chunk:
            batch.add(service.users().messages().get(userId="me", id=msg["id"]))
    
        batch.execute() 
        time.sleep(0.3)
    return found_emails


def render_email_html(email_data):
    unique_matches = ", ".join(set(email_data['matches']))
    count = len(email_data['matches'])
    
    return (
        f"<div style='margin-bottom: 20px; border-bottom: 1px solid #ccc; padding-bottom: 10px;'>"
        f"  <span style='color: #006400;'><strong>Matched:</strong> {unique_matches} ({count})</span><br>"
        f"  <strong>From:</strong> {email_data['from']}<br>"
        f"  <strong>Subject:</strong> {email_data['subject']}<br>"
        f"  <p style='color: #666;'>{email_data['body_content'][:150]}...</p>"
        f"</div>"
    )




@app.route("/protected_area")
@login_is_required
def protected_area():
    creds = Credentials(**session["credentials"])
    service = build("gmail", "v1", credentials=creds)
    
    query = "newer_than:90d"
    all_raw_messages = []
    next_page_token = None

    while True:
        results = service.users().messages().list(
            userId="me", 
            q=query, 
            maxResults=500, 
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
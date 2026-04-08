import os
import pathlib
import base64
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
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    }


def login_is_required(function):
    def wrapper(*args, **kwargs):
        if "google_id" not in session:
            return abort(401)  # Authorization required
        else:
            return function()

    return wrapper


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
    session['credentials'] = credentials_dict(credentials)
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


@app.route("/protected_area")
@login_is_required
def protected_area():
    if 'credentials' not in session:
        return abort(401)

    creds = Credentials(**session['credentials'])
    service = build('gmail', 'v1', credentials=creds)

    results = service.users().messages().list(
        userId='me',
        maxResults=5
    ).execute()

    messages = results.get('messages', [])
    emails = []

    for msg in messages:
        message = service.users().messages().get(
            userId='me',
            id=msg['id'],
            format='raw'
        ).execute()

        raw_data = base64.urlsafe_b64decode(message['raw'])
        email_text = raw_data.decode('utf-8', errors='ignore')

        emails.append(email_text[:1000])  # truncate for sanity

    return {
        "user": session["name"],
        "emails": emails
    }

if __name__ == "__main__":
    app.run(host="localhost", port=5000, debug=True)
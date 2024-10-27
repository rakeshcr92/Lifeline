import os
import requests
import sqlite3
from flask import Flask, request, jsonify, render_template
from datetime import datetime
from pymongo import MongoClient
from bson.objectid import ObjectId
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.twiml.messaging_response import MessagingResponse
from geopy.geocoders import Nominatim
from sinch import SinchClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Initialize Flask app
app = Flask(__name__)

# API and service keys from environment variables
openai.api_key = os.getenv("OPENAI_API_KEY")
client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
mongo_client = MongoClient(os.getenv("MONGO_URI"), tlsCAFile=certifi.where())
mongo_collection = mongo_client["emergencies_db"]["emergencies_collection"]

sinch_client = SinchClient(
    key_id=os.getenv("SINCH_KEY_ID"),
    key_secret=os.getenv("SINCH_KEY_SECRET"),
    project_id=os.getenv("SINCH_PROJECT_ID")
)

# SQLite database initialization
def init_db():
    with sqlite3.connect('emergency_data.db') as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS emergencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, summary TEXT, 
            location TEXT, level TEXT, tone1 TEXT, tone1_percentage INTEGER, 
            tone2 TEXT, tone2_percentage INTEGER, latitude REAL, longitude REAL, 
            time TEXT, assigned_units TEXT DEFAULT 'N/A')''')

# Backup emergency data to MongoDB
def backup_to_mongodb(data):
    try:
        mongo_collection.insert_one(data)
    except Exception as e:
        print(f"MongoDB Error: {e}")

# Extract emergency info using GPT
def extract_emergency_info(text):
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": f"Extract emergency info: {text}"}]
        )
        return eval(response.choices[0].message.content.strip())
    except Exception as e:
        print(f"GPT Error: {e}")

# Get coordinates from Google API
def get_coordinates(address):
    try:
        response = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": os.getenv("GOOGLE_API_KEY")}
        )
        loc = response.json()['results'][0]['geometry']['location']
        return loc['lat'], loc['lng']
    except Exception as e:
        print(f"Geocode Error: {e}")

# Flask Routes
@app.route("/sms", methods=['POST'])
def receive_sms():
    msg_body = request.form.get('Body')
    info = extract_emergency_info(msg_body)
    response = MessagingResponse()
    response.message("Thank you for contacting 911. Help is on the way.")
    print(f"SMS received: {msg_body}")
    return str(response)

@app.route("/voice", methods=['POST'])
def voice():
    resp = VoiceResponse()
    resp.say("911, state your name, location, and emergency.")
    resp.record(action="/process-info", play_beep=True)
    return str(resp)

@app.route("/process-info", methods=['POST'])
def process_info():
    resp = VoiceResponse()
    gather = Gather(input="speech", action="/handle-response")
    gather.say("Are you in a safe location?")
    resp.append(gather)
    resp.say("Emergency responders are on their way.")
    return str(resp)

@app.route("/recording-complete", methods=['POST'])
def recording_complete():
    url = request.form.get("RecordingUrl")
    sid = request.form.get("CallSid")
    filename = f"call_{sid}.mp3"
    download_audio(url, filename)
    transcription = transcribe_audio(filename)
    if transcription:
        extract_and_store_info(transcription)
    return '', 200

def download_audio(url, filename):
    try:
        response = requests.get(f"{url}.mp3", auth=(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")))
        with open(filename, 'wb') as f:
            f.write(response.content)
    except Exception as e:
        print(f"Download Error: {e}")

def transcribe_audio(filename):
    try:
        with open(filename, "rb") as f:
            return openai.Audio.transcribe("whisper-1", f)['text']
    except Exception as e:
        print(f"Transcription Error: {e}")

def extract_and_store_info(text):
    info = extract_emergency_info(text)
    lat, lng = get_coordinates(info['location'])
    data = {
        **info, "latitude": lat, "longitude": lng, 
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 
        "assigned_units": "N/A"
    }
    with sqlite3.connect('emergency_data.db') as conn:
        conn.execute('''INSERT INTO emergencies (title, summary, location, level, tone1, 
                         tone1_percentage, tone2, tone2_percentage, latitude, longitude, time) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                         tuple(data.values()))
    backup_to_mongodb(data)

@app.route("/api/emergencies", methods=['GET'])
def get_emergencies():
    try:
        emergencies = list(mongo_collection.find())
        return jsonify({'emergencies': [
            {**e, 'id': str(e['_id'])} for e in emergencies]})
    except Exception as e:
        print(f"Fetch Error: {e}")
        return jsonify({'error': 'Unable to fetch emergencies'}), 500

@app.route("/api/dispatch", methods=['POST'])
def dispatch_unit():
    data = request.get_json()
    emergency = mongo_collection.find_one({'_id': ObjectId(data['emergency_id'])})
    if not emergency:
        return jsonify({'error': 'Emergency not found'}), 404
    units = f"{emergency.get('assigned_units', 'N/A')}, {data['unit_type']} {data['unit_id']}"
    mongo_collection.update_one({'_id': ObjectId(data['emergency_id'])}, {'$set': {'assigned_units': units}})
    sinch_client.sms.batches.send(body=f"Dispatched {units}", to=[os.getenv("RESPONDER_PHONE")])
    return jsonify({'status': 'success'})

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == "__main__":
    init_db()
    app.run(debug=True)

import os
import requests
import uuid
import re
import tempfile
from enum import Enum
from pathlib import Path
from urllib.parse import urlencode
import assemblyai as aai
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Record, Play, Say

load_dotenv()
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY")
CAMB_API_KEY = os.getenv("CAMB_API_KEY")
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8000")

# Setup
aai.settings.api_key = ASSEMBLYAI_API_KEY
TEMP_AUDIO_DIR = Path(tempfile.gettempdir()) / "riya_audio"
TEMP_AUDIO_DIR.mkdir(exist_ok=True)

# Twilio client
twilio_client = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_SID and TWILIO_TOKEN else None

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ConversationState(str, Enum):
    GREETING = "greeting"
    INTEREST_CHECK = "interest_check"
    EXPERIENCE_CHECK = "experience_check"
    FRESHER_QUALIFICATION = "fresher_qualification"
    EXP_DETAILS = "exp_details"
    CUSTOMER_STORY = "customer_story"
    CUSTOMER_RETRY = "customer_retry"
    FESTIVAL_STORY = "festival_story"
    FESTIVAL_RETRY = "festival_retry"
    COMPLETED = "completed"
    REJECTED = "rejected"

class SessionData:
    def __init__(self, phone_number=None):
        self.conversation = []
        self.state = ConversationState.GREETING
        self.candidate_type = None
        self.retry_count = 0
        self.answers = {}
        self.phone_number = phone_number
        self.current_audio_url = None

def generate_tts(text: str, session_id: str) -> str:
    """Generate TTS using CambAI REST API."""
    try:
        audio_id = f"{session_id}_{uuid.uuid4().hex[:8]}.wav"
        audio_path = TEMP_AUDIO_DIR / audio_id
        
        url = "https://client.camb.ai/apis/tts-stream"
        headers = {
            "x-api-key": CAMB_API_KEY,
            "Content-Type": "application/json"
        }
        payload = {
            "text": text,
            "language": "en-us",
            "voice_id": 147320,
            "speech_model": "mars-flash"
        }
        
        response = requests.post(url, json=payload, headers=headers, stream=True, timeout=60)
        
        if response.status_code == 200:
            with open(audio_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            # Return public URL for Twilio to access
            return f"{PUBLIC_URL}/audio/{audio_id}"
        return None
    except Exception as e:
        print(f"TTS Error: {e}")
        return None

def check_story_quality(text: str) -> bool:
    sentences = re.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    return len(sentences) >= 8 or len(text.split()) >= 50

def get_reply(session: SessionData, user_input: str) -> str:
    user_lower = user_input.lower().strip()
    current_state = session.state
    
    if current_state == ConversationState.GREETING:
        session.state = ConversationState.INTEREST_CHECK
        return "Hi, this is Riya from Futuresoft Consultancy. We are hiring voice and chat profiles for companies like British Telecom, Teleperformance, and Wipro. Are you interested?"
    
    elif current_state == ConversationState.INTEREST_CHECK:
        if any(word in user_lower for word in ['no', 'not', 'nah', 'nope']):
            return "I understand. Thank you for your time. Have a great day!"
        elif any(word in user_lower for word in ['yes', 'yeah', 'sure', 'interested', 'ok']):
            session.state = ConversationState.EXPERIENCE_CHECK
            return "We will surely help you with the same. Could you please confirm me if you are Fresher OR Experienced?"
        else:
            return "Could you please confirm if you are interested? Just say Yes or No."
    
    elif current_state == ConversationState.EXPERIENCE_CHECK:
        if any(word in user_lower for word in ['fresher', 'fresh', 'student']):
            session.candidate_type = 'fresher'
            session.state = ConversationState.FRESHER_QUALIFICATION
            return "Now, what's your highest qualification like Graduate, Undergraduate, or Graduation drop-out?"
        elif any(word in user_lower for word in ['experience', 'experienced', 'worked']):
            session.candidate_type = 'experienced'
            session.state = ConversationState.EXP_DETAILS
            return "Now, please confirm your highest qualification and experience. Mention your job responsibility part clearly."
        else:
            return "Could you please clarify - are you a Fresher or Experienced?"
    
    elif current_state == ConversationState.FRESHER_QUALIFICATION:
        session.answers['qualification'] = user_input
        session.state = ConversationState.CUSTOMER_STORY
        return "That was very impressive. Could you please speak about any memorable interaction with customer within 10 to 12 sentences. You can start with, 'Once a customer called me for issue related to...' And your time starts now."
    
    elif current_state == ConversationState.EXP_DETAILS:
        session.answers['experience'] = user_input
        session.state = ConversationState.CUSTOMER_STORY
        return "That was very impressive. Could you please speak about any memorable interaction with customer within 10 to 12 sentences. You can start with, 'Once a customer called me for issue related to...' And your time starts now."
    
    elif current_state == ConversationState.CUSTOMER_STORY:
        is_valid = check_story_quality(user_input)
        if is_valid:
            session.answers['customer_story'] = user_input
            session.state = ConversationState.FESTIVAL_STORY
            return "Acknowledgment to statement. Could you please speak about any latest festival you celebrated like Diwali, Holi, Christmas or Eid in 10 to 12 sentences. Start with, 'I celebrated my last Diwali along with family...' And your time starts now."
        else:
            session.retry_count += 1
            if session.retry_count >= 2:
                session.state = ConversationState.REJECTED
                return "Sorry, we will not be able to help you with job as we hire candidates with good communication skills only."
            else:
                session.state = ConversationState.CUSTOMER_RETRY
                return "Sorry, you need to speak only 10 to 12 sentences on this topic. It can be done within 15 seconds only. Please speak on this topic now."
    
    elif current_state == ConversationState.CUSTOMER_RETRY:
        is_valid = check_story_quality(user_input)
        if is_valid:
            session.answers['customer_story'] = user_input
            session.state = ConversationState.FESTIVAL_STORY
            return "Acknowledgment to statement. Could you please speak about any latest festival you celebrated like Diwali, Holi, Christmas or Eid in 10 to 12 sentences. Start with, 'I celebrated my last Diwali along with family...' And your time starts now."
        else:
            session.state = ConversationState.REJECTED
            return "Sorry, we will not be able to help you with job as we hire candidates with good communication skills only."
    
    elif current_state == ConversationState.FESTIVAL_STORY:
        is_valid = check_story_quality(user_input)
        if is_valid:
            session.answers['festival'] = user_input
            session.state = ConversationState.COMPLETED
            return "That was amazing, now one of our HR Recruiter will connect you for your further interview process."
        else:
            session.retry_count += 1
            if session.retry_count >= 2:
                session.state = ConversationState.REJECTED
                return "Sorry, we will not be able to help you with job as we hire candidates with good communication skills only."
            else:
                session.state = ConversationState.FESTIVAL_RETRY
                return "Sorry, please speak clearly about the festival celebration for 10 to 12 sentences to proceed."
    
    elif current_state == ConversationState.FESTIVAL_RETRY:
        is_valid = check_story_quality(user_input)
        if is_valid:
            session.answers['festival'] = user_input
            session.state = ConversationState.COMPLETED
            return "That was amazing, now one of our HR Recruiter will connect you for your further interview process."
        else:
            session.state = ConversationState.REJECTED
            return "Sorry, we will not be able to help you with job as we hire candidates with good communication skills only."
    
    elif current_state in [ConversationState.REJECTED, ConversationState.COMPLETED]:
        return "Thank you for your time. Have a great day!"
    
    return "I'm sorry, could you please repeat that?"

# Store sessions
sessions = {}

@app.get("/")
def read_root():
    return FileResponse("index.html")

@app.get("/audio/{audio_id}")
def get_audio(audio_id: str):
    audio_path = TEMP_AUDIO_DIR / audio_id
    if audio_path.exists():
        return FileResponse(audio_path, media_type="audio/wav")
    raise HTTPException(status_code=404, detail="Audio not found")

# ================= TWILIO CALL HANDLING =================

@app.post("/initiate-call")
async def initiate_call(phone_number: str = Form(...)):
    """Initiate a call to the specified phone number."""
    if not twilio_client:
        raise HTTPException(status_code=500, detail="Twilio not configured")
    
    # Create session for this call
    session_id = str(uuid.uuid4())
    session = SessionData(phone_number=phone_number)
    sessions[session_id] = session
    
    # Generate opening audio
    opening_text = get_reply(session, "")
    audio_url = generate_tts(opening_text, session_id)
    session.current_audio_url = audio_url
    
    try:
        call = twilio_client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE,
            url=f"{PUBLIC_URL}/twilio-webhook?session_id={session_id}",
            status_callback=f"{PUBLIC_URL}/call-status?session_id={session_id}",
            status_callback_event=["completed", "answered"],
            machine_detection="Enable"
        )
        
        return {
            "success": True, 
            "call_sid": call.sid, 
            "session_id": session_id,
            "message": f"Calling {phone_number}..."
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

@app.post("/twilio-webhook")
async def twilio_webhook(
    request: Request, 
    session_id: str = None, 
    RecordingUrl: str = None,
    CallStatus: str = None
):
    """Handle Twilio webhooks with error recovery."""
    response = VoiceResponse()
    
    print(f"Webhook called - Session: {session_id}, Recording: {RecordingUrl}, Status: {CallStatus}")
    
    if not session_id or session_id not in sessions:
        print("ERROR: Session not found")
        response.say("Sorry, this session has expired. Please call again.")
        return Response(content=str(response), media_type="application/xml")
    
    session = sessions[session_id]
    
    # Process recording if present
    if RecordingUrl:
        try:
            print(f"Downloading recording from: {RecordingUrl}")
            # Download recording
            audio_content = requests.get(RecordingUrl, timeout=30).content
            
            # Save temporarily
            with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as tmp:
                tmp.write(audio_content)
                tmp_path = tmp.name
            
            print("Transcribing with AssemblyAI...")
            # Transcribe
            config = aai.TranscriptionConfig(
                speech_models=["universal-2"], 
                language_code="en_us"
            )
            transcriber = aai.Transcriber(config=config)
            transcript = transcriber.transcribe(tmp_path)
            
            # Cleanup
            os.unlink(tmp_path)
            
            if transcript.status == "error":
                print(f"Transcription error: {transcript.error}")
                user_input = ""
            else:
                user_input = transcript.text
                print(f"User said: {user_input}")
                
        except Exception as e:
            print(f"Transcription failed: {e}")
            user_input = ""
    else:
        print("No recording, first call")
        user_input = ""
    
    # Get reply from conversation flow
    try:
        if not user_input or not user_input.strip():
            if session.state in [ConversationState.CUSTOMER_STORY, ConversationState.FESTIVAL_STORY]:
                session.retry_count += 1
                if session.retry_count >= 2:
                    session.state = ConversationState.REJECTED
                    reply = "Sorry, we will not be able to help you with job as we hire candidates with good communication skills only."
                else:
                    if session.state == ConversationState.CUSTOMER_STORY:
                        session.state = ConversationState.CUSTOMER_RETRY
                    else:
                        session.state = ConversationState.FESTIVAL_RETRY
                    reply = "Sorry, you need to speak on this topic. Please try now."
            else:
                reply = "I didn't catch that. Could you please speak up?"
        else:
            reply = get_reply(session, user_input)
        
        print(f"Riya replies: {reply}")
        
    except Exception as e:
        print(f"Conversation flow error: {e}")
        reply = "I'm sorry, could you please repeat that?"
    
    # Generate TTS with fallback
    audio_url = None
    try:
        if reply:
            audio_url = generate_tts(reply, session_id)
            session.current_audio_url = audio_url
    except Exception as e:
        print(f"TTS generation failed: {e}")
        audio_url = None
    
    # Build TwiML response
    if audio_url:
        print(f"Playing audio: {audio_url}")
        response.play(audio_url)
    else:
        # Fallback to Twilio's native TTS if CambAI fails
        print("Using fallback TTS")
        response.say(reply, voice="Polly.Joanna", language="en-US")
    
    # Continue or hang up
    if session.state not in [ConversationState.REJECTED, ConversationState.COMPLETED]:
        print("Recording next response...")
        response.record(
            action=f"{PUBLIC_URL}/twilio-webhook?session_id={session_id}",
            max_length=60,  # Increased to 60 seconds
            play_beep=True,
            trim="trim-silence",
            timeout=5
        )
    else:
        print("Conversation ended")
        response.hangup()
    
    twiml_content = str(response)
    print(f"TwiML: {twiml_content[:200]}...")
    
    return Response(content=twiml_content, media_type="application/xml")

@app.post("/call-status")
async def call_status(session_id: str = None, CallStatus: str = None):
    """Handle call status callbacks."""
    print(f"Call status for {session_id}: {CallStatus}")
    if session_id in sessions and CallStatus in ["completed", "busy", "failed"]:
        # Cleanup old sessions
        pass
    return {"status": "ok"}

# ================= WEB INTERFACE =================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Riya Voice Agent - Make Calls</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; background: #f0f2f5; }
            .container { background: white; padding: 30px; border-radius: 15px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
            h1 { color: #1a73e8; text-align: center; }
            input[type="tel"] { 
                width: 100%; padding: 15px; font-size: 18px; 
                border: 2px solid #ddd; border-radius: 10px; margin: 20px 0;
                box-sizing: border-box;
            }
            button { 
                width: 100%; padding: 15px; background: #1a73e8; color: white; 
                border: none; border-radius: 10px; font-size: 18px; cursor: pointer;
            }
            button:hover { background: #1557b0; }
            button:disabled { background: #ccc; }
            .status { margin-top: 20px; padding: 15px; border-radius: 8px; text-align: center; display: none; }
            .success { background: #e8f5e9; color: #2e7d32; }
            .error { background: #ffebee; color: #c62828; }
            .info { background: #e3f2fd; color: #1565c0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎙️ Riya Voice Recruiter</h1>
            <h3 style="text-align: center; color: #666;">Make AI Interview Calls</h3>
            
            <input type="tel" id="phoneNumber" placeholder="Enter Indian mobile number (+91XXXXXXXXXX)" value="+91">
            <button onclick="makeCall()" id="callBtn">📞 Make Call</button>
            
            <div id="status" class="status"></div>
            
            <div style="margin-top: 30px; padding: 15px; background: #fff3e0; border-radius: 8px; font-size: 14px;">
                <strong>Note:</strong> 
                <ul>
                    <li>Use format: +91XXXXXXXXXX</li>
                    <li>Requires Twilio account with Indian number</li>
                    <li>Free trial available with $15.50 credits</li>
                </ul>
            </div>
        </div>

        <script>
            async function makeCall() {
                const phone = document.getElementById('phoneNumber').value.trim();
                const btn = document.getElementById('callBtn');
                const status = document.getElementById('status');
                
                if (!phone || phone.length < 10) {
                    showStatus('Please enter a valid phone number', 'error');
                    return;
                }
                
                btn.disabled = true;
                btn.textContent = 'Calling...';
                showStatus('Initiating call...', 'info');
                
                try {
                    const formData = new FormData();
                    formData.append('phone_number', phone);
                    
                    const res = await fetch('/initiate-call', {
                        method: 'POST',
                        body: formData
                    });
                    
                    const data = await res.json();
                    
                    if (data.success) {
                        showStatus(`✅ Call initiated! SID: ${data.call_sid}`, 'success');
                    } else {
                        showStatus(`❌ Error: ${data.error}`, 'error');
                    }
                } catch (e) {
                    showStatus(`❌ Error: ${e.message}`, 'error');
                } finally {
                    btn.disabled = false;
                    btn.textContent = '📞 Make Call';
                }
            }
            
            function showStatus(msg, type) {
                const status = document.getElementById('status');
                status.textContent = msg;
                status.className = 'status ' + type;
                status.style.display = 'block';
            }
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)


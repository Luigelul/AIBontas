from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
import asyncio
import sqlite3
import requests
import json
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

app = FastAPI(title="AI Social Cross-Poster API (Text, Link, Audio)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Inițializăm noul client Google GenAI (SDK-ul modern)
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))


# --- SETĂRI BAZĂ DE DATE ---
def init_db():
    conn = sqlite3.connect("monitorizare.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS posts_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_post TEXT,
            linkedin_post TEXT,
            twitter_post TEXT,
            facebook_post TEXT,
            status TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()


init_db()


def save_to_db(master, linkedin, twitter, facebook, status):
    conn = sqlite3.connect("monitorizare.db")
    cursor = conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT INTO posts_log (master_post, linkedin_post, twitter_post, facebook_post, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (master, linkedin, twitter, facebook, status, timestamp))
    conn.commit()
    conn.close()


# --- MODELE DE DATE ---
class PostRequest(BaseModel):
    master_post: str


class PublishRequest(BaseModel):
    linkedin_text: str
    twitter_text: str
    facebook_text: str


# --- FUNCȚIE UTILITARĂ: EXTRAGERE TEXT DIN LINK ---
def extract_text_from_url(url: str) -> str:
    try:
        response = requests.get(url, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        paragraphs = soup.find_all('p')
        text = ' '.join([p.get_text() for p in paragraphs])
        return text[:3000] if len(text) > 3000 else text
    except Exception as e:
        raise Exception(f"Nu am putut citi articolul de pe link. Eroare: {str(e)}")


# --- FUNCȚIA DE GENERARE AI (Pentru Text) ---
async def generate_social_post(master_post: str, platform: str, system_prompt: str, retries: int = 3) -> str:
    for attempt in range(retries):
        try:
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"Informația de bază: {master_post}",
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7
                )
            )
            return response.text
        except Exception as e:
            eroare = str(e)
            if "503" in eroare or "UNAVAILABLE" in eroare:
                if attempt < retries - 1:
                    await asyncio.sleep(2)
                    continue
            return f"Eroare la generarea pentru {platform}: {eroare}"


# --- ENDPOINT-URI PENTRU PROCESARE ---

# A. Procesare TEXT Simplu
@app.post("/api/generate-posts")
async def generate_posts(request: PostRequest):
    if not request.master_post:
        raise HTTPException(status_code=400, detail="Textul nu poate fi gol.")
    return await process_content_to_social(request.master_post)


# B. Procesare LINK
@app.post("/api/generate-from-link")
async def generate_from_link(request: PostRequest):
    if not request.master_post:
        raise HTTPException(status_code=400, detail="Link-ul nu poate fi gol.")
    try:
        extracted_text = extract_text_from_url(request.master_post)
        return await process_content_to_social(f"Creează postări bazate pe acest articol: {extracted_text}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# C. Procesare AUDIO (Evităm File API, trimitem datele direct INLINE)
@app.post("/api/generate-from-audio")
async def generate_from_audio(audio_file: UploadFile = File(...)):
    try:
        # 1. Citim fișierul audio direct în memoria RAM sub formă de bytes (fără să-l salvăm pe disc)
        audio_bytes = await audio_file.read()

        # 2. Prompt-ul magic
        prompt = """
        Ascultă această înregistrare audio. Extrage ideea principală și generează 3 postări de social media.
        Returnează rezultatul strict în format JSON cu cheile: "linkedin", "twitter", "facebook".

        Reguli pentru LinkedIn: Profesionist, orientat spre valoare, 3-4 hashtag-uri.
        Reguli pentru Twitter: Maxim 280 caractere, incisiv, 1-2 hashtag-uri.
        Reguli pentru Facebook: Prietenos, folosește emoji-uri, termină cu o întrebare.
        """

        # 3. Generăm conținutul asincron (folosim Part.from_bytes pentru a injecta audio-ul direct)
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                prompt,
                types.Part.from_bytes(
                    data=audio_bytes,
                    mime_type=audio_file.content_type or "audio/webm"
                )
            ]
        )

        try:
            # Curățăm textul în cazul în care Gemini adaugă formatare Markdown (```json ... ```)
            clean_text = response.text.replace("```json", "").replace("```", "").strip()
            results = json.loads(clean_text)

            save_to_db("Sursă: Audio", results.get("linkedin", ""), results.get("twitter", ""),
                       results.get("facebook", ""), "GENERAT_AUDIO")

            return {
                "status": "success",
                "results": results
            }
        except Exception as e:
            return {"status": "error", "message": f"Nu s-a putut parsa răspunsul AI: {response.text}"}

    except Exception as e:
        print(f"EROARE DETALIATĂ AUDIO: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Funcția centrală care coordonează textele (folosită de Text și Link)
async def process_content_to_social(content: str):
    prompt_linkedin = "Ești un copywriter expert. Rescrie pentru LinkedIn. Ton: Profesionist. Adaugă hashtag-uri. REGULA: Fără introduceri, dă-mi doar textul postării."
    prompt_twitter = "Rescrie pentru Twitter. Maxim 280 de caractere. Direct și concis. REGULA: Fără introduceri, dă-mi doar textul postării."
    prompt_facebook = "Rescrie pentru Facebook. Ton: Prietenos. Folosește emoji. Încheie cu o întrebare. REGULA: Fără introduceri, dă-mi doar textul postării."

    task_linkedin = generate_social_post(content, "LinkedIn", prompt_linkedin)
    task_twitter = generate_social_post(content, "Twitter", prompt_twitter)
    task_facebook = generate_social_post(content, "Facebook", prompt_facebook)

    linkedin_post, twitter_post, facebook_post = await asyncio.gather(task_linkedin, task_twitter, task_facebook)
    save_to_db(content[:50], linkedin_post, twitter_post, facebook_post, "GENERAT_CU_SUCCES")

    return {
        "status": "success",
        "results": {
            "linkedin": linkedin_post,
            "twitter": twitter_post,
            "facebook": facebook_post
        }
    }


# --- ENDPOINT-URI PENTRU DASHBOARD & PUBLICARE N8N ---
@app.get("/api/logs")
async def get_logs():
    conn = sqlite3.connect("monitorizare.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, master_post, status, timestamp FROM posts_log ORDER BY id DESC LIMIT 10")
    logs = cursor.fetchall()
    conn.close()

    formatted_logs = [{"id": r[0], "master_post": r[1][:50] + "...", "status": r[2], "timestamp": r[3]} for r in logs]
    return {"logs": formatted_logs}


@app.post("/api/publish")
async def publish_to_socials(request: PublishRequest):
    N8N_WEBHOOK_URL = "https://aibontasluigi.app.n8n.cloud/webhook-test/webhook-endpoint"  # Atenție la webhook-ul de test vs producție în n8n!
    try:
        payload = {"linkedin": request.linkedin_text, "twitter": request.twitter_text,
                   "facebook": request.facebook_text}
        response = requests.post(N8N_WEBHOOK_URL, json=payload)
        if response.status_code == 200:
            return {"status": "success", "message": "Trimis cu succes către n8n!"}
        else:
            return {"status": "error", "message": f"Eroare n8n: {response.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
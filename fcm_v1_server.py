import json
import os
import tempfile
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google.oauth2 import service_account
from google.auth.transport.requests import Request

PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "respect-app-dbc77")

# محليًا على Windows استخدم FIREBASE_SERVICE_ACCOUNT أو المسار الافتراضي.
# على Render استخدم FIREBASE_SERVICE_ACCOUNT_JSON وضع محتوى ملف respect-app.json كاملًا.
SERVICE_ACCOUNT_FILE = os.getenv("FIREBASE_SERVICE_ACCOUNT", r"C:\keys\respect-app.json")
SERVICE_ACCOUNT_JSON = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://oafbzceorbjykgoffuaa.supabase.co").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "sb_publishable_UXfOau7Th8Nu3Vs85a-7-g_Xn8Tjt0S")
APP_SHARED_SECRET = os.getenv("APP_SHARED_SECRET", "")

SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]

app = FastAPI(title="Respect App FCM HTTP v1 Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_secret(x_app_secret: Optional[str]) -> None:
    if APP_SHARED_SECRET and x_app_secret != APP_SHARED_SECRET:
        raise HTTPException(status_code=401, detail="Invalid X-App-Secret")


def _load_service_account_info() -> Dict[str, Any]:
    """
    يدعم طريقتين:
    1) Render/VPS: FIREBASE_SERVICE_ACCOUNT_JSON = محتوى ملف JSON كامل.
    2) Windows local: FIREBASE_SERVICE_ACCOUNT أو C:\\keys\\respect-app.json.
    """
    if SERVICE_ACCOUNT_JSON:
        try:
            return json.loads(SERVICE_ACCOUNT_JSON)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Invalid FIREBASE_SERVICE_ACCOUNT_JSON: {e}",
            )

    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise HTTPException(
            status_code=500,
            detail=f"Service account file not found: {SERVICE_ACCOUNT_FILE}",
        )

    try:
        with open(SERVICE_ACCOUNT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cannot read service account file: {e}")


def get_access_token() -> str:
    info = _load_service_account_info()
    try:
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        creds.refresh(Request())
        return creds.token
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create Firebase access token: {e}")


def normalize_username(value: str) -> str:
    return value.strip().lower().replace("@", "")


def display_username(value: str) -> str:
    clean = normalize_username(value)
    return f"@{clean}" if clean else "@user"


def get_user_fcm_token(receiver_username: str) -> Optional[str]:
    clean = normalize_username(receiver_username)
    display = display_username(clean)

    url = f"{SUPABASE_URL}/rest/v1/users"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    params = {
        "select": "username,fcm_token",
        "or": f"(username.eq.{clean},username.eq.{display})",
        "limit": "1",
    }

    response = requests.get(url, headers=headers, params=params, timeout=15)
    if response.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"Supabase error: {response.text}")

    rows = response.json()
    if not rows:
        return None

    token = rows[0].get("fcm_token")
    return str(token).strip() if token else None


class PushRequest(BaseModel):
    token: str
    type: str = Field(default="message")
    title: str
    body: str
    data: Dict[str, Any] = Field(default_factory=dict)


class UserPushRequest(BaseModel):
    receiverUsername: str
    type: str = Field(default="message")
    title: str
    body: str
    data: Dict[str, Any] = Field(default_factory=dict)


class MessagePushRequest(BaseModel):
    receiverUsername: str
    senderUsername: str
    senderName: str = ""
    messageId: str
    text: str = ""


class CallPushRequest(BaseModel):
    receiverUsername: str
    callId: str
    callerUsername: str
    callerName: str = "مستخدم"
    callerAvatar: str = ""
    video: bool = False


def send_fcm_v1(token: str, msg_type: str, title: str, body: str, data: Dict[str, Any]) -> Dict[str, Any]:
    token = token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing FCM token")

    access_token = get_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{PROJECT_ID}/messages:send"

    clean_data = {
        str(k): "" if v is None else str(v)
        for k, v in {**data, "type": msg_type, "title": title, "body": body}.items()
    }

    channel_id = "respect_calls_channel" if msg_type == "call" else "respect_messages_channel"

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body,
            },
            "data": clean_data,
            "android": {
                "priority": "HIGH",
                "notification": {
                    "channel_id": channel_id,
                    "sound": "default",
                },
            },
        }
    }

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=payload,
        timeout=20,
    )

    print("========== FCM RESPONSE ==========")
    print("STATUS:", response.status_code)
    print("BODY:", response.text)
    print("==================================")

    if response.status_code >= 400:
        raise HTTPException(
            status_code=400,
            detail={
                "firebase_status": response.status_code,
                "firebase_body": response.text,
                "hint": "SENDER_ID_MISMATCH يعني google-services.json أو service account من مشروع مختلف. UNREGISTERED يعني التوكن قديم.",
            },
        )

    return {"ok": True, "firebase": response.json()}


@app.get("/")
def health():
    service_account_source = "env:FIREBASE_SERVICE_ACCOUNT_JSON" if SERVICE_ACCOUNT_JSON else SERVICE_ACCOUNT_FILE
    return {
        "ok": True,
        "project": PROJECT_ID,
        "service_account_source": service_account_source,
        "using_service_account_json_env": bool(SERVICE_ACCOUNT_JSON),
        "service_account_file_exists": os.path.exists(SERVICE_ACCOUNT_FILE),
    }


@app.post("/send_push")
def send_push(req: PushRequest, x_app_secret: Optional[str] = Header(default=None)):
    _check_secret(x_app_secret)
    return send_fcm_v1(req.token, req.type, req.title, req.body, req.data)


@app.post("/send_user_push")
def send_user_push(req: UserPushRequest, x_app_secret: Optional[str] = Header(default=None)):
    _check_secret(x_app_secret)
    token = get_user_fcm_token(req.receiverUsername)
    if not token:
        raise HTTPException(status_code=400, detail="receiver_has_no_fcm_token")
    return send_fcm_v1(token, req.type, req.title, req.body, req.data)


@app.post("/send_message_push")
def send_message_push(req: MessagePushRequest, x_app_secret: Optional[str] = Header(default=None)):
    _check_secret(x_app_secret)
    title = req.senderName.strip() or display_username(req.senderUsername)
    body = req.text.strip() or "أرسل لك رسالة"

    token = get_user_fcm_token(req.receiverUsername)
    if not token:
        raise HTTPException(status_code=400, detail="receiver_has_no_fcm_token")

    return send_fcm_v1(
        token,
        "message",
        title,
        body,
        {
            "messageId": req.messageId,
            "senderUsername": display_username(req.senderUsername),
            "senderName": req.senderName,
            "text": req.text,
            "peerUsername": display_username(req.senderUsername),
            "peerName": title,
        },
    )


@app.post("/send_call_push")
def send_call_push(req: CallPushRequest, x_app_secret: Optional[str] = Header(default=None)):
    _check_secret(x_app_secret)
    title = "مكالمة فيديو واردة" if req.video else "مكالمة صوتية واردة"
    body = req.callerName.strip() or display_username(req.callerUsername)

    token = get_user_fcm_token(req.receiverUsername)
    if not token:
        raise HTTPException(status_code=400, detail="receiver_has_no_fcm_token")

    return send_fcm_v1(
        token,
        "call",
        title,
        body,
        {
            "callId": req.callId,
            "callerUsername": display_username(req.callerUsername),
            "callerName": req.callerName,
            "callerAvatarPath": req.callerAvatar,
            "video": str(req.video).lower(),
            "call_type": "video" if req.video else "audio",
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("fcm_v1_server:app", host="0.0.0.0", port=8000, reload=True)

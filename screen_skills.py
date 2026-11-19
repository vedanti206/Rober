"""
screen_skills.py  -  Rober's eyes and hands for the screen.

What it can do:
  skip_ad()                  click the YouTube "Skip" button when it appears
  read_screen_text()         read the words on the window in front (OCR)
  explain_screen(question)   read the window and explain it with the local AI
  prepare_chat_reply(text)   read your friend's last WhatsApp message and get a reply
                             ready. It does NOT send anything. Rober must ask you
                             yes/no first, and only then call result["send"]().

Privacy: screenshots stay in memory. Nothing is saved to disk or uploaded.
Safety:  Rober only clicks inside the app he was asked to work in.
         Move the mouse to a screen corner to stop him at any time (pyautogui failsafe).

Test it by hand (open the window you want first, you get 5 seconds):
  python screen_skills.py skip
  python screen_skills.py read
  python screen_skills.py explain
  python screen_skills.py chat
"""
import os
import re
import sys
import time

import pyautogui
import pyperclip
import pytesseract
import requests
from mss import mss
from PIL import Image

try:
    import pygetwindow as gw
except Exception:          # pygetwindow missing or not working
    gw = None

try:
    import config
    OLLAMA_URL = config.OLLAMA_URL
    OLLAMA_MODEL = config.OLLAMA_MODEL
except Exception:
    OLLAMA_URL = "http://localhost:11434"
    OLLAMA_MODEL = "llama3.2:3b"

# Where Tesseract was installed (the default place on Windows)
_TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(_TESS):
    pytesseract.pytesseract.tesseract_cmd = _TESS

pyautogui.FAILSAFE = True   # mouse into a corner = emergency stop
pyautogui.PAUSE = 0.15

# Rober only types and clicks in windows whose title contains one of these words
CHAT_APPS = ["whatsapp"]


# ---------------------------------------------------------------------------
# Windows and screenshots
# ---------------------------------------------------------------------------
def front_window():
    """The biggest window at the front (skips Rober himself, he is small).
    Returns (title, left, top, width, height) or None."""
    if gw is None:
        return None
    try:
        for w in gw.getAllWindows():
            if not w.title or not w.visible or w.isMinimized:
                continue
            if w.width < 300 or w.height < 250:
                continue
            return (w.title, w.left, w.top, w.width, w.height)
    except Exception:
        pass
    return None


def _clip(left, top, width, height):
    """Keep a rectangle inside the screen."""
    sw, sh = pyautogui.size()
    l, t = max(0, int(left)), max(0, int(top))
    r, b = min(sw, int(left + width)), min(sh, int(top + height))
    return l, t, max(1, r - l), max(1, b - t)


def grab(region):
    """Take a screenshot of region=(left, top, width, height). Kept in memory only."""
    l, t, w, h = region
    with mss() as sct:
        shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


# ---------------------------------------------------------------------------
# Reading text (OCR)
# ---------------------------------------------------------------------------
def ocr_words(img, scale=2):
    """Read an image. Returns a list of words with their position."""
    big = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    data = pytesseract.image_to_data(big, output_type=pytesseract.Output.DICT)
    words = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < 30:
            continue
        words.append({
            "text": txt,
            "x": data["left"][i] / scale,
            "y": data["top"][i] / scale,
            "w": data["width"][i] / scale,
            "h": data["height"][i] / scale,
            "line": (data["block_num"][i], data["par_num"][i], data["line_num"][i]),
        })
    return words


def group_lines(words):
    """Join words into lines of text, top to bottom."""
    by_line = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w)
    lines = []
    for ws in by_line.values():
        ws.sort(key=lambda a: a["x"])
        lines.append({
            "text": " ".join(a["text"] for a in ws),
            "x": min(a["x"] for a in ws),
            "y": min(a["y"] for a in ws),
            "h": max(a["h"] for a in ws),
            "words": ws,
        })
    lines.sort(key=lambda ln: ln["y"])
    return lines


# ---------------------------------------------------------------------------
# 1) Skip the YouTube ad
# ---------------------------------------------------------------------------
_SKIP_WORD = re.compile(r"\bskip(?:\s+ads?)?\b", re.I)
_SKIP_COUNTDOWN = re.compile(r"skip\s+(?:in\s+)?\d", re.I)   # "Skip in 5" = not ready yet


def find_skip_button(lines):
    """Look through the lines of text for the Skip button. Returns (x, y) or None."""
    for ln in lines:
        text = ln["text"]
        if len(text) > 18:                    # a long line is not a button
            continue
        if not _SKIP_WORD.search(text) or _SKIP_COUNTDOWN.search(text):
            continue
        for w in ln["words"]:
            if w["text"].lower().startswith("skip"):
                return w["x"] + w["w"] / 2, w["y"] + w["h"] / 2
    return None


def skip_ad(wait_seconds=15):
    """Wait for the Skip button and click it. Returns a sentence for Rober to say."""
    end = time.time() + wait_seconds
    while time.time() < end:
        win = front_window()
        if win is None:
            return "I can't see which window is open."
        title, l, t, w, h = win
        if "youtube" not in title.lower():
            return "YouTube is not the window in front, so I did not click anything."
        # The Skip button lives in the lower right part of the video
        region = _clip(l + w * 0.40, t + h * 0.35, w * 0.60, h * 0.65)
        img = grab(region)
        spot = find_skip_button(group_lines(ocr_words(img)))
        if spot:
            old = pyautogui.position()
            pyautogui.click(region[0] + spot[0], region[1] + spot[1])
            pyautogui.moveTo(old)
            time.sleep(1)
            return "Skipped the ad."
        time.sleep(1)
    return "I did not see a Skip button. The ad may not be skippable yet."


# ---------------------------------------------------------------------------
# 2) Read and explain the screen
# ---------------------------------------------------------------------------
def read_screen_text(max_chars=2500):
    """Read all the text in the window at the front."""
    win = front_window()
    if win is None:
        return ""
    _, l, t, w, h = win
    img = grab(_clip(l, t, w, h))
    lines = group_lines(ocr_words(img))
    text = "\n".join(ln["text"] for ln in lines)
    return text[:max_chars]


def _ask_llm(system, prompt, timeout=180):
    """Ask the local AI (Ollama). Returns '' if the brain is asleep."""
    try:
        r = requests.post(
            OLLAMA_URL + "/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "options": {"num_predict": 200},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["content"].strip()
    except Exception:
        return ""


def explain_screen(question=None):
    """Read the window and explain it in simple English."""
    text = read_screen_text()
    if not text.strip():
        return "I could not read any words on the screen."
    system = ("You are Rober, a friendly puppy helper. The text below was read from "
              "the user's screen by OCR, so it can have small mistakes. Answer in simple "
              "English, short and clear (under 80 words).")
    prompt = f"SCREEN TEXT:\n{text}\n\nUSER REQUEST: {question or 'Explain this and give the answer if there is a question.'}"
    answer = _ask_llm(system, prompt)
    return answer or "My brain is asleep. Open Ollama and try again."


# ---------------------------------------------------------------------------
# 3) Read a friend's chat message and reply
# ---------------------------------------------------------------------------
_TIME_AT_END = re.compile(r"\s*\d{1,2}:\d{2}\s*(?:am|pm)?\s*\S{0,3}$", re.I)
_JUNK_LINE = re.compile(
    r"^(?:\d{1,2}:\d{2}\s*(?:am|pm)?|today|yesterday|online|typing\.*|"
    r"type a message|messages are end-to-end encrypted.*)$", re.I)


def pick_friend_lines(lines, pane_width):
    """From the lines of the chat pane, return the friend's latest message(s).
    Rule used: the friend's bubbles start near the LEFT edge, yours start further right."""
    left_limit = pane_width * 0.20
    picked = []
    for ln in reversed(lines):                       # start from the bottom
        text = _TIME_AT_END.sub("", ln["text"]).strip()
        if not text or _JUNK_LINE.match(text):
            continue
        if ln["x"] <= left_limit:                    # friend
            picked.append(text)
            if len(picked) >= 6:
                break
        elif picked:                                 # reached my own bubble
            break
    picked.reverse()
    return " ".join(picked).strip()


def _chat_pane(win):
    """The chat part of the WhatsApp window (the right side, without header and input bar)."""
    _, l, t, w, h = win
    return _clip(l + w * 0.36, t + h * 0.12, w * 0.64, h * 0.78)


def read_friend_message(win):
    region = _chat_pane(win)
    img = grab(region)
    lines = group_lines(ocr_words(img))
    return pick_friend_lines(lines, img.width)


def draft_reply(friend_text):
    system = ("You write short, warm, natural text-message replies for the user. "
              "Maximum 25 words. Reply with only the message text, no quotes, no "
              "explanation. Use the same language style as the friend (English or Hinglish).")
    answer = _ask_llm(system, f"My friend sent me: {friend_text}\nWrite my reply.")
    return answer.strip().strip('"').strip("'")


def send_chat_message(text, win):
    """Type the text in the chat box and press Enter. Call this ONLY after the user said yes."""
    now = front_window()
    if now is None or now[0] != win[0]:
        return "The window changed, so I did not send anything."
    _, l, t, w, h = win
    # Find the box by its words, otherwise click where it usually is
    bar = _clip(l + w * 0.36, t + h * 0.85, w * 0.64, h * 0.15)
    spot = None
    for ln in group_lines(ocr_words(grab(bar))):
        if "type a message" in ln["text"].lower() or ln["text"].lower().startswith("type a"):
            spot = (bar[0] + ln["x"] + 40, bar[1] + ln["y"] + ln["h"] / 2)
            break
    if spot is None:
        spot = (l + w * 0.65, t + h - max(30, h * 0.05))
    pyautogui.click(*spot)
    old_clip = pyperclip.paste()
    try:
        pyperclip.copy(text)                          # paste works for Hindi and emojis too
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)
        pyautogui.press("enter")
    finally:
        time.sleep(0.3)
        pyperclip.copy(old_clip)
    return "Sent."


def prepare_chat_reply(reply_text=None):
    """Read the friend's message and get the reply ready (nothing is sent yet).
    Returns a dict:
      on error:  {"error": "sentence for Rober to say"}
      on success: {"friend_said", "reply", "ask", "send"}   (call result["send"]() after YES)
    """
    win = front_window()
    if win is None:
        return {"error": "I can't see which window is open."}
    if not any(app in win[0].lower() for app in CHAT_APPS):
        return {"error": "Open the WhatsApp chat first, then ask me again."}
    friend = read_friend_message(win)
    if not friend:
        return {"error": "I could not read your friend's message. Open the chat and scroll to the latest message."}
    reply = (reply_text or "").strip()
    if not reply:
        reply = draft_reply(friend)
        if not reply:
            return {"error": "My brain is asleep, so I can't write a reply. Tell me what to say."}
    return {
        "friend_said": friend,
        "reply": reply,
        "ask": f'Your friend said: "{friend}". Should I send: "{reply}"? Say yes or no.',
        "send": lambda: send_chat_message(reply, win),
    }


# ---------------------------------------------------------------------------
# Hand testing:  python screen_skills.py skip | read | explain | chat
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "read"
    print("Click the window you want me to work on. Starting in 5 seconds...")
    time.sleep(5)
    if what == "skip":
        print(skip_ad())
    elif what == "explain":
        print(explain_screen())
    elif what == "chat":
        res = prepare_chat_reply()
        if "error" in res:
            print(res["error"])
        else:
            print(res["ask"])
            if input("yes/no: ").strip().lower() in ("y", "yes"):
                print(res["send"]())
            else:
                print("Okay, I did not send it.")
    else:
        print(read_screen_text())

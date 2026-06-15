# -*- coding: utf-8 -*-
"""
Ayuda para averiguar tu chat_id.
1. Abre Telegram y manda CUALQUIER mensaje a @ResultadosMundial_bot.
2. Ejecuta:  python get_chat_id.py
Te imprime el chat_id que tienes que poner en TELEGRAM_CHAT_ID.
"""
import os
import requests

TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    TOKEN = input("Pega el token del bot de Telegram: ").strip()

r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=30).json()
chats = {}
for upd in r.get("result", []):
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat")
    if chat:
        chats[chat["id"]] = chat.get("first_name") or chat.get("title") or chat.get("username")

if not chats:
    print("No hay mensajes todavía. Manda un mensaje al bot y vuelve a ejecutar.")
else:
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")

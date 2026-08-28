import asyncio
import hashlib
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from html import escape
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from config.settings import settings
from storage.postgres.db_connect import create_database, create_tables, update_history_messages
from telegram_bot.handlers import asr
from telegram_bot.handlers.pdf_reader import read_pdf
from telegram_bot.handlers.txt_reader import read_txt
from zettelkasten.atomizer import NoteAtomizer
from zettelkasten.graph_rag import GraphRAG
from zettelkasten.graph_visualizer import generate_graph_html_from_repo
from zettelkasten.linker import GraphLinker, LinkAction, LocalEmbeddingModel

load_dotenv()

app = FastAPI(title="Executive Exocortex Web App")

create_database()
create_tables()

embedding_model = LocalEmbeddingModel(model_name=settings.embedding_model_name)
atomizer = NoteAtomizer(
    model_name=settings.zettel_atomizer_model_name,
    temperature=settings.zettel_atomizer_temperature,
    system_prompt=settings.zettel_atomizer_system_prompt,
    user_prompt_template=settings.zettel_atomizer_user_prompt_template,
)
linker = GraphLinker(
    embedding_model=embedding_model,
    model_name=settings.linker_model_name,
    temperature=settings.linker_temperature,
    system_prompt=settings.linker_system_prompt,
    user_prompt_template=settings.linker_user_prompt_template,
    similarity_threshold=settings.linker_similarity_threshold,
    max_candidates=settings.linker_max_candidates,
)
graphrag = GraphRAG(
    embedding_model=embedding_model,
    model_name=settings.graphrag_model_name,
    temperature=settings.graphrag_temperature,
    system_prompt=settings.graphrag_system_prompt,
    user_prompt_template=settings.graphrag_user_prompt_template,
    no_context_response=settings.graphrag_no_context_response,
    similarity_threshold=settings.graphrag_similarity_threshold,
)

DELETE_CACHE: dict[str, list[dict]] = {}
DEMO_USERS = {"admin": "admin123", "demo": "demo", "nastya": "1234"}


def build_user_id(raw_user: str) -> str:
    clean = "".join(ch for ch in raw_user.strip() if ch.isalnum() or ch in "_-.")
    return f"web_{clean or 'demo'}"


def pseudo_numeric_user_id(raw_user: str) -> int:
    return abs(hash(raw_user)) % 2_000_000_000


def save_user_note(user_id: str, text: str) -> tuple[bool, str]:
    raw_cards = atomizer.atomize(
        text=text,
        current_db_max_root_id=linker.repository.get_max_root_id(user_id),
    )
    if isinstance(raw_cards, str):
        return False, f"Ошибка: {raw_cards}"
    results = linker.link_and_insert(user_id=user_id, new_cards=raw_cards)
    actions_count = {a: 0 for a in LinkAction}
    for r in results:
        actions_count[r.action] += 1
    stats = linker.get_user_stats(user_id)
    return True, f"✅ Записано в граф знаний.\n📚 Размер базы: {stats['total_cards']} карточек"


def log_event(raw_user: str, message_text: str, message_type: str, bot_answer: str) -> None:
    try:
        update_history_messages(pseudo_numeric_user_id(raw_user), int(time.time() * 1000),
            message_text, datetime.now(), message_type, bot_answer)
    except Exception as e:
        print(f"[web_app] log warning: {e}")


def get_user(request: Request) -> str | None:
    return request.cookies.get("web_user")


def check_auth(request: Request) -> bool:
    return request.cookies.get("web_auth") == "1"


def format_llm_response(text: str) -> str:
    """Format LLM response - supports both HTML and markdown."""
    
    # Check if text already contains HTML tags
    has_html = bool(re.search(r'<(b|i|strong|em|p|br|ul|ol|li|h[1-6]|code|pre|a|span|div)\b', text, re.IGNORECASE))
    
    if has_html:
        # Text already has HTML - just sanitize dangerous tags but keep safe ones
        # Remove script, style, iframe, etc.
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<iframe[^>]*>.*?</iframe>', '', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<(script|style|iframe|object|embed|form|input|button)[^>]*/?>', '', text, flags=re.IGNORECASE)
        
        # Convert <b> to <strong>, <i> to <em> for consistency
        text = re.sub(r'<b\b([^>]*)>', r'<strong\1>', text, flags=re.IGNORECASE)
        text = re.sub(r'</b>', '</strong>', text, flags=re.IGNORECASE)
        text = re.sub(r'<i\b([^>]*)>', r'<em\1>', text, flags=re.IGNORECASE)
        text = re.sub(r'</i>', '</em>', text, flags=re.IGNORECASE)
        
        # Handle [number] references - make them smaller/styled
        text = re.sub(r'\[(\d+(?:\.\d+)?)\]', r'<sup class="ref">[\1]</sup>', text)
        
        # Ensure line breaks are preserved
        text = text.replace('\n\n', '</p><p>')
        text = text.replace('\n', '<br>')
        
        # Wrap in paragraph if not already wrapped
        if not text.strip().startswith('<p') and not text.strip().startswith('<h') and not text.strip().startswith('<ul') and not text.strip().startswith('<ol'):
            text = f'<p>{text}</p>'
    else:
        # No HTML - apply markdown formatting
        # Escape HTML first
        text = escape(text)
        
        # Bold: **text** or __text__
        text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
        
        # Italic: *text* or _text_
        text = re.sub(r'\*([^*]+?)\*', r'<em>\1</em>', text)
        text = re.sub(r'_([^_]+?)_', r'<em>\1</em>', text)
        
        # Code: `code`
        text = re.sub(r'`([^`]+?)`', r'<code>\1</code>', text)
        
        # Headers: # ## ###
        text = re.sub(r'^### (.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
        text = re.sub(r'^## (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
        text = re.sub(r'^# (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
        
        # Lists: - item or * item or numbered
        text = re.sub(r'^[\-\*] (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
        text = re.sub(r'^(\d+)\. (.+)$', r'<li>\2</li>', text, flags=re.MULTILINE)
        
        # Wrap consecutive <li> in <ul>
        text = re.sub(r'((?:<li>.+?</li>\n?)+)', r'<ul>\1</ul>', text)
        
        # Handle [number] references
        text = re.sub(r'\[(\d+(?:\.\d+)?)\]', r'<sup class="ref">[\1]</sup>', text)
        
        # Line breaks
        text = text.replace('\n\n', '</p><p>')
        text = text.replace('\n', '<br>')
        text = f'<p>{text}</p>'
    
    # Clean up empty paragraphs
    text = re.sub(r'<p>\s*</p>', '', text)
    text = re.sub(r'<p>\s*<(h[234]|ul|ol)', r'<\1', text)
    text = re.sub(r'</(h[234]|ul)>\s*</p>', r'</\1>', text)
    
    return text


CSS = """
:root {
  --bg: #0a0a0f; --card: #12121a; --card2: #1a1a24;
  --text: #fff; --text2: #e5e5e5; --muted: #6b7280; 
  --accent: #6366f1; --accent2: #a855f7;
  --success: #10b981; --error: #ef4444; --border: #1f1f2e;
  --glow: rgba(99,102,241,0.15);
  --code-bg: #1e1e2e;
}
[data-theme="light"] {
  --bg: #f8fafc; --card: #ffffff; --card2: #f1f5f9;
  --text: #0f172a; --text2: #334155; --muted: #64748b;
  --border: #e2e8f0; --glow: rgba(99,102,241,0.1);
  --code-bg: #f1f5f9;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
  background: var(--bg); color: var(--text); min-height: 100vh;
  transition: background 0.3s, color 0.3s;
}
[data-theme="dark"] body {
  background-image: radial-gradient(ellipse 80% 50% at 50% -20%, rgba(99,102,241,0.15), transparent);
}
.container { max-width: 480px; margin: 0 auto; padding: 20px 16px; min-height: 100vh; }
.container.wide { max-width: 600px; }

/* Theme toggle */
.theme-toggle { position: fixed; top: 16px; right: 16px; z-index: 100; background: var(--card); border: 1px solid var(--border); border-radius: 50%; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center; cursor: pointer; font-size: 20px; transition: all 0.2s; }
.theme-toggle:hover { transform: scale(1.1); }

/* Auth */
.auth-wrap { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
.auth-box { width: 100%; max-width: 360px; }
.logo { text-align: center; margin-bottom: 32px; }
.logo h1 { font-size: 28px; background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.logo p { color: var(--muted); font-size: 14px; margin-top: 6px; }
.auth-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 28px 24px; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; color: var(--muted); font-size: 13px; margin-bottom: 6px; }
.form-group input { width: 100%; padding: 14px 16px; background: var(--bg); border: 1px solid var(--border); border-radius: 10px; color: var(--text); font-size: 15px; transition: border 0.2s; }
.form-group input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); }
.btn { display: block; width: 100%; padding: 14px; background: linear-gradient(135deg, var(--accent), var(--accent2)); border: none; border-radius: 10px; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; transition: transform 0.1s, opacity 0.2s; }
.btn:hover { opacity: 0.9; }
.btn:active { transform: scale(0.98); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-outline { background: transparent; border: 1px solid var(--border); color: var(--text); }
.btn-outline:hover { background: var(--card2); }
.btn-danger { background: var(--error); }
.error-msg { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); color: var(--error); padding: 12px; border-radius: 8px; font-size: 14px; margin-bottom: 16px; text-align: center; }

/* Header */
.header { text-align: center; padding: 28px 0 20px; }
.header h1 { font-size: 26px; background: linear-gradient(135deg, var(--accent), var(--accent2)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
.header p { color: var(--muted); font-size: 14px; margin-top: 4px; }
.user-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 16px; background: var(--card); border: 1px solid var(--border); border-radius: 20px; font-size: 13px; color: var(--muted); margin-bottom: 16px; }
.user-pill a { color: var(--accent); text-decoration: none; margin-left: 8px; }
.user-pill a:hover { text-decoration: underline; }

/* Welcome */
.welcome { background: linear-gradient(135deg, var(--card) 0%, var(--card2) 100%); border: 1px solid var(--border); border-radius: 16px; padding: 20px; margin-bottom: 20px; }
.welcome h3 { font-size: 15px; margin-bottom: 12px; color: var(--text); }
.welcome ul { margin: 8px 0 14px 18px; color: var(--text2); font-size: 14px; line-height: 1.7; }
.welcome p { color: var(--text2); font-size: 14px; line-height: 1.6; }

/* Menu */
.menu-btn { display: flex; align-items: center; gap: 14px; width: 100%; padding: 18px 20px; margin-bottom: 12px; background: var(--card); border: 1px solid var(--border); border-radius: 14px; color: var(--text); font-size: 15px; text-decoration: none; transition: all 0.2s; position: relative; overflow: hidden; }
.menu-btn::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: linear-gradient(135deg, var(--accent), var(--accent2)); opacity: 0; transition: opacity 0.2s; }
.menu-btn:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 8px 24px var(--glow); }
.menu-btn:hover::before { opacity: 0.08; }
.menu-btn .icon { font-size: 22px; width: 32px; text-align: center; position: relative; z-index: 1; }
.menu-btn span { position: relative; z-index: 1; }

/* Page */
.page-header { display: flex; align-items: center; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--border); margin-bottom: 20px; }
.page-header h2 { font-size: 18px; font-weight: 500; }
.back-link { color: var(--accent); text-decoration: none; font-size: 14px; display: inline-flex; align-items: center; gap: 4px; margin-bottom: 12px; }
.back-link:hover { text-decoration: underline; }
.msg-box { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; color: var(--text2); font-size: 14px; }

/* Forms */
textarea { width: 100%; padding: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; color: var(--text); font-size: 14px; resize: vertical; min-height: 100px; font-family: inherit; }
textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--glow); }
input[type="file"] { width: 100%; padding: 14px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; color: var(--text); font-size: 14px; }
input[type="file"]::file-selector-button { background: var(--accent); color: #fff; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; margin-right: 12px; }

/* Tabs */
.tabs { display: flex; gap: 8px; margin-bottom: 16px; }
.tab { flex: 1; padding: 12px; background: var(--card); border: 1px solid var(--border); border-radius: 10px; color: var(--muted); font-size: 14px; text-align: center; cursor: pointer; transition: all 0.2s; }
.tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
.tab-content { display: none; }
.tab-content.active { display: block; }

/* Alerts */
.alert { padding: 14px 16px; border-radius: 12px; margin-bottom: 16px; font-size: 14px; display: flex; align-items: flex-start; gap: 10px; }
.alert-success { background: rgba(16,185,129,0.1); border: 1px solid rgba(16,185,129,0.3); color: var(--success); }
.alert-error { background: rgba(239,68,68,0.1); border: 1px solid rgba(239,68,68,0.3); color: var(--error); }

/* Chat */
.chat-container { display: flex; flex-direction: column; height: calc(100vh - 200px); min-height: 400px; }
.chat-messages { flex: 1; overflow-y: auto; padding: 16px 0; display: flex; flex-direction: column; gap: 12px; }
.chat-msg { max-width: 85%; padding: 14px 18px; border-radius: 18px; font-size: 14px; line-height: 1.6; animation: fadeIn 0.3s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.chat-msg.user { background: linear-gradient(135deg, var(--accent), var(--accent2)); color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; }
.chat-msg.bot { background: var(--card); border: 1px solid var(--border); align-self: flex-start; border-bottom-left-radius: 4px; color: var(--text); }
.chat-msg .meta { font-size: 11px; color: var(--muted); margin-top: 8px; opacity: 0.8; }
.chat-msg h2, .chat-msg h3, .chat-msg h4 { margin: 12px 0 8px; font-size: 15px; }
.chat-msg h2 { font-size: 17px; }
.chat-msg ul { margin: 8px 0; padding-left: 20px; }
.chat-msg li { margin: 4px 0; }
.chat-msg code { background: var(--code-bg); padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.chat-msg strong { font-weight: 600; }
.chat-msg em { font-style: italic; }
.chat-msg p { margin: 8px 0; }
.chat-msg p:first-child { margin-top: 0; }
.chat-msg p:last-child { margin-bottom: 0; }
.chat-msg .ref { font-size: 10px; color: var(--accent); opacity: 0.7; vertical-align: super; margin: 0 1px; }
.chat-input-wrap { display: flex; gap: 10px; padding-top: 16px; border-top: 1px solid var(--border); }
.chat-input-wrap textarea { flex: 1; min-height: 48px; max-height: 120px; padding: 12px 16px; }
.chat-input-wrap .btn { width: auto; padding: 12px 24px; }

/* Delete cards */
.delete-cards { display: flex; flex-direction: column; gap: 12px; margin: 16px 0; }
.delete-card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; cursor: pointer; transition: all 0.2s; }
.delete-card:hover { border-color: var(--accent); transform: translateX(4px); }
.delete-card .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.delete-card .card-id { font-size: 12px; color: var(--accent); font-weight: 500; }
.delete-card .card-score { font-size: 11px; color: var(--muted); background: var(--card2); padding: 2px 8px; border-radius: 10px; }
.delete-card .card-preview { font-size: 14px; color: var(--text2); line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* Loading */
.loading-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(10,10,15,0.92); display: none; align-items: center; justify-content: center; z-index: 1000; backdrop-filter: blur(8px); }
[data-theme="light"] .loading-overlay { background: rgba(248,250,252,0.92); }
.loading-overlay.active { display: flex; }
.loading-box { text-align: center; }
.spinner { width: 56px; height: 56px; border: 3px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite; margin: 0 auto 16px; }
@keyframes spin { to { transform: rotate(360deg); } }
.loading-text { color: var(--text); font-size: 15px; font-weight: 500; }
.loading-subtext { color: var(--muted); font-size: 13px; margin-top: 8px; }
.loading-dots::after { content: ''; animation: dots 1.5s steps(4) infinite; }
@keyframes dots { 0% { content: ''; } 25% { content: '.'; } 50% { content: '..'; } 75% { content: '...'; } }
.loading-progress { width: 200px; height: 4px; background: var(--border); border-radius: 2px; margin: 16px auto 0; overflow: hidden; }
.loading-progress-bar { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); width: 30%; animation: progress 1.5s ease-in-out infinite; }
@keyframes progress { 0% { transform: translateX(-100%); } 100% { transform: translateX(400%); } }

/* Particles */
.particles { position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; z-index: -1; }
[data-theme="light"] .particles { opacity: 0.5; }
.particle { position: absolute; width: 4px; height: 4px; background: var(--accent); border-radius: 50%; opacity: 0.3; animation: float 15s infinite; }
@keyframes float { 0%, 100% { transform: translateY(100vh) rotate(0deg); opacity: 0; } 10% { opacity: 0.3; } 90% { opacity: 0.3; } }

/* Modal */
.modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(10,10,15,0.92); display: none; align-items: center; justify-content: center; z-index: 1000; backdrop-filter: blur(8px); padding: 20px; }
[data-theme="light"] .modal-overlay { background: rgba(248,250,252,0.92); }
.modal-overlay.active { display: flex; }
.modal-box { background: var(--card); border: 1px solid var(--border); border-radius: 20px; padding: 28px; max-width: 450px; width: 100%; animation: modalIn 0.25s ease; }
@keyframes modalIn { from { opacity: 0; transform: scale(0.95) translateY(10px); } to { opacity: 1; transform: scale(1) translateY(0); } }
.modal-box h3 { margin-bottom: 12px; font-size: 18px; }
.modal-box p { color: var(--muted); font-size: 14px; margin-bottom: 16px; }
.modal-box .quote { background: var(--card2); padding: 16px; border-radius: 12px; margin-bottom: 20px; font-size: 14px; line-height: 1.6; color: var(--text2); max-height: 200px; overflow-y: auto; border-left: 3px solid var(--accent); }
.modal-box .card-id { font-size: 12px; color: var(--accent); margin-bottom: 8px; font-weight: 500; }
.modal-btns { display: flex; gap: 12px; }
.modal-btns button { flex: 1; padding: 14px; border-radius: 10px; font-size: 14px; font-weight: 500; cursor: pointer; border: none; transition: all 0.2s; }
.modal-btns .cancel { background: var(--card2); color: var(--text); border: 1px solid var(--border); }
.modal-btns .cancel:hover { background: var(--border); }
.modal-btns .confirm { background: var(--error); color: #fff; }
.modal-btns .confirm:hover { opacity: 0.9; }
"""

JS_COMMON = """
// Theme
function initTheme() {
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    updateThemeIcon();
}
function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateThemeIcon();
}
function updateThemeIcon() {
    const btn = document.querySelector('.theme-toggle');
    if (btn) {
        const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
        btn.textContent = isDark ? '☀️' : '🌙';
    }
}
initTheme();

// Loading
function showLoading(text, subtext) {
    document.getElementById('loadingText').textContent = text || 'Обработка';
    const sub = document.getElementById('loadingSubtext');
    if (sub) sub.textContent = subtext || '';
    document.getElementById('loadingOverlay').classList.add('active');
}
function hideLoading() {
    document.getElementById('loadingOverlay').classList.remove('active');
}
function submitWithLoading(form, text, subtext) {
    const textarea = form.querySelector('textarea');
    const fileInput = form.querySelector('input[type="file"]');
    if (textarea && !textarea.value.trim()) {
        textarea.focus();
        return false;
    }
    if (fileInput && fileInput.files.length === 0) {
        return false;
    }
    showLoading(text, subtext);
    return true;
}

// Enter to submit
document.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        const active = document.activeElement;
        if (active && active.tagName === 'TEXTAREA') {
            const form = active.closest('form');
            if (form && form.dataset.enterSubmit === 'true') {
                e.preventDefault();
                if (submitWithLoading(form, form.dataset.loadingText || 'Обработка', form.dataset.loadingSubtext || '')) {
                    form.submit();
                }
            }
        }
    }
});

// Particles
(function() {
    const container = document.querySelector('.particles');
    if (!container) return;
    for (let i = 0; i < 15; i++) {
        const p = document.createElement('div');
        p.className = 'particle';
        p.style.left = Math.random() * 100 + '%';
        p.style.animationDelay = Math.random() * 15 + 's';
        p.style.animationDuration = (12 + Math.random() * 8) + 's';
        container.appendChild(p);
    }
})();
"""


def html_page(title: str, body: str, extra_js: str = "", show_theme_toggle: bool = True) -> str:
    theme_btn = '<button class="theme-toggle" onclick="toggleTheme()">🌙</button>' if show_theme_toggle else ''
    return f"""<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="particles"></div>
{theme_btn}
<div id="loadingOverlay" class="loading-overlay">
    <div class="loading-box">
        <div class="spinner"></div>
        <div class="loading-text"><span id="loadingText">Обработка</span><span class="loading-dots"></span></div>
        <div class="loading-subtext" id="loadingSubtext"></div>
        <div class="loading-progress"><div class="loading-progress-bar"></div></div>
    </div>
</div>
{body}
<script>{JS_COMMON}{extra_js}</script>
</body>
</html>"""


# ========== AUTH ==========

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/home", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if check_auth(request):
        return RedirectResponse("/home", status_code=303)
    
    error_html = f'<div class="error-msg">{escape(error)}</div>' if error else ""
    
    body = f"""
    <div class="container auth-wrap">
        <div class="auth-box">
            <div class="logo">
                <h1>Executive Exocortex</h1>
                <p>Цифровой экзокортекс топ-менеджера</p>
            </div>
            <div class="auth-card">
                {error_html}
                <form action="/login" method="post" onsubmit="showLoading('Вход в систему')">
                    <div class="form-group">
                        <label>Логин</label>
                        <input type="text" name="username" placeholder="Введите логин" required autofocus>
                    </div>
                    <div class="form-group">
                        <label>Пароль</label>
                        <input type="password" name="password" placeholder="Введите пароль" required>
                    </div>
                    <button type="submit" class="btn">Войти</button>
                </form>
            </div>
        </div>
    </div>
    """
    return HTMLResponse(html_page("Вход — Executive Exocortex", body))


@app.post("/login")
async def login_submit(username: str = Form(""), password: str = Form("")):
    username = username.strip()
    password = password.strip()
    
    if not username or not password:
        return RedirectResponse("/login?error=Введите логин и пароль", status_code=303)
    
    if DEMO_USERS.get(username) == password:
        resp = RedirectResponse("/home", status_code=303)
        resp.set_cookie("web_user", username, max_age=86400*30)
        resp.set_cookie("web_auth", "1", max_age=86400*30)
        return resp
    
    return RedirectResponse("/login?error=Неверный логин или пароль", status_code=303)


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("web_user")
    resp.delete_cookie("web_auth")
    return resp


# ========== HOME ==========

@app.get("/home", response_class=HTMLResponse)
async def home_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    user = get_user(request)
    
    body = f"""
    <div class="container">
        <div class="header">
            <h1>Executive Exocortex</h1>
            <p>Ваша персональная база знаний</p>
        </div>
        
        <div class="user-pill">
            👤 {escape(user or 'demo')}
            <a href="/logout">Выйти</a>
        </div>
        
        <div class="welcome">
            <h3>📥 Отправляйте:</h3>
            <ul>
                <li>голосовые сообщения</li>
                <li>текстовые заметки</li>
                <li>документы и файлы</li>
            </ul>
            <h3>🧠 Система автоматически:</h3>
            <ul>
                <li>распознает и анализирует информацию</li>
                <li>связывает заметки по смыслу</li>
                <li>формирует персональный граф знаний</li>
            </ul>
            <p>🔍 Задавайте вопросы и получайте релевантную информацию из базы знаний.</p>
        </div>
        
        <a href="/add" class="menu-btn"><span class="icon">➕</span><span>Добавить новую заметку</span></a>
        <a href="/search" class="menu-btn"><span class="icon">🔍</span><span>Поиск мыслей по запросу</span></a>
        <a href="/view" class="menu-btn"><span class="icon">💡</span><span>Посмотреть базу знаний</span></a>
        <a href="/delete" class="menu-btn"><span class="icon">🗑</span><span>Удалить заметку</span></a>
    </div>
    """
    return HTMLResponse(html_page("Executive Exocortex", body))


# ========== ADD ==========

@app.get("/add", response_class=HTMLResponse)
async def add_page(request: Request, msg: str = "", st: str = ""):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    alert = ""
    if msg:
        cls = "alert-success" if st == "ok" else "alert-error"
        alert = f'<div class="alert {cls}">{escape(msg)}</div>'
    
    body = f"""
    <div class="container">
        <a href="/home" class="back-link">← На главную</a>
        <div class="page-header"><h2>➕ Добавить заметку</h2></div>
        
        {alert}
        
        <div class="msg-box">Отправьте текст или загрузите файл для записи в базу знаний.</div>
        
        <div class="tabs">
            <div class="tab active" onclick="showTab('text', this)">📝 Текст</div>
            <div class="tab" onclick="showTab('file', this)">📄 Файл</div>
            <div class="tab" onclick="showTab('voice', this)">🎤 Голос</div>
        </div>
        
        <div id="tab-text" class="tab-content active">
            <form action="/add/text" method="post" data-enter-submit="true" data-loading-text="Сохранение заметки" data-loading-subtext="Анализ и добавление в граф знаний" onsubmit="return submitWithLoading(this, 'Сохранение заметки', 'Анализ и добавление в граф знаний')">
                <div class="form-group"><textarea name="note_text" placeholder="Введите заметку..." required></textarea></div>
                <button type="submit" class="btn">Сохранить</button>
            </form>
        </div>
        
        <div id="tab-file" class="tab-content">
            <form action="/add/file" method="post" enctype="multipart/form-data" onsubmit="return submitWithLoading(this, 'Обработка файла', 'Извлечение текста и анализ содержимого')">
                <div class="form-group"><label>Файл (.pdf / .txt)</label><input type="file" name="file" accept=".pdf,.txt" required></div>
                <button type="submit" class="btn">Обработать</button>
            </form>
        </div>
        
        <div id="tab-voice" class="tab-content">
            <form action="/add/voice" method="post" enctype="multipart/form-data" onsubmit="return submitWithLoading(this, 'Распознавание речи', 'Конвертация и транскрибация аудио')">
                <div class="form-group"><label>Аудиофайл</label><input type="file" name="file" accept="audio/*" required></div>
                <button type="submit" class="btn">Распознать</button>
            </form>
        </div>
    </div>
    """
    
    js = """
    function showTab(name, el) {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        el.classList.add('active');
        document.getElementById('tab-' + name).classList.add('active');
    }
    """
    return HTMLResponse(html_page("Добавить", body, js))


@app.post("/add/text")
async def add_text(request: Request, note_text: str = Form("")):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    user = get_user(request)
    text = note_text.strip()
    if not text:
        return RedirectResponse("/add?msg=Введите текст заметки&st=err", status_code=303)
    ok, ans = save_user_note(build_user_id(user or "demo"), text)
    log_event(user or "demo", text, "text_artifact", ans)
    return RedirectResponse(f"/add?msg={escape(ans)}&st={'ok' if ok else 'err'}", status_code=303)


@app.post("/add/file")
async def add_file(request: Request, file: UploadFile = File(None)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    user = get_user(request)
    
    if not file or not file.filename:
        return RedirectResponse("/add?msg=Выберите файл&st=err", status_code=303)
    
    ext = Path(file.filename).suffix.lower()
    if ext not in {".pdf", ".txt"}:
        return RedirectResponse("/add?msg=Поддерживаются .pdf и .txt&st=err", status_code=303)
    
    tmp = Path(tempfile.gettempdir()) / f"web_{uuid.uuid4().hex}{ext}"
    try:
        with tmp.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        if ext == ".pdf":
            data = read_pdf(str(tmp))
            text = "\n".join(str(v) for v in data.values() if v)
        else:
            text = read_txt(str(tmp))
        if not text.strip():
            return RedirectResponse("/add?msg=Текст не извлечен&st=err", status_code=303)
        ok, ans = save_user_note(build_user_id(user or "demo"), text)
        log_event(user or "demo", f"[{file.filename}]", ext[1:].upper(), ans)
        return RedirectResponse(f"/add?msg={escape(ans)}&st={'ok' if ok else 'err'}", status_code=303)
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/add/voice")
async def add_voice(request: Request, file: UploadFile = File(None)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    user = get_user(request)
    
    if not file or not file.filename:
        return RedirectResponse("/add?msg=Выберите аудиофайл&st=err", status_code=303)
    
    ext = Path(file.filename).suffix.lower() or ".bin"
    inp = Path(tempfile.gettempdir()) / f"web_v_{uuid.uuid4().hex}{ext}"
    wav = Path(tempfile.gettempdir()) / f"web_v_{uuid.uuid4().hex}.wav"
    try:
        with inp.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        if ext != ".wav":
            proc = await asyncio.create_subprocess_exec("ffmpeg", "-i", str(inp), str(wav), "-y",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.communicate()
            if proc.returncode != 0:
                return RedirectResponse("/add?msg=Ошибка конвертации аудио&st=err", status_code=303)
        else:
            wav = inp
        text = await asyncio.get_running_loop().run_in_executor(None, asr.recognize_audio, str(wav))
        ok, ans = save_user_note(build_user_id(user or "demo"), text)
        msg = f"🎤 \"{text[:100]}{'...' if len(text)>100 else ''}\"\n\n{ans}"
        log_event(user or "demo", text, "voice", ans)
        return RedirectResponse(f"/add?msg={escape(msg)}&st={'ok' if ok else 'err'}", status_code=303)
    finally:
        inp.unlink(missing_ok=True)
        if wav != inp:
            wav.unlink(missing_ok=True)


# ========== SEARCH (Chat style with formatting) ==========

@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    body = """
    <div class="container wide">
        <a href="/home" class="back-link">← На главную</a>
        <div class="page-header"><h2>🔍 Поиск мыслей</h2></div>
        
        <div class="chat-container">
            <div class="chat-messages" id="chatMessages">
                <div class="chat-msg bot">
                    <p>Задайте вопрос, и я найду релевантную информацию из вашей базы знаний.</p>
                </div>
            </div>
            
            <form class="chat-input-wrap" id="searchForm" onsubmit="return sendMessage(event)">
                <textarea id="queryInput" name="q" placeholder="Введите вопрос..." rows="1"></textarea>
                <button type="submit" class="btn" id="sendBtn">→</button>
            </form>
        </div>
    </div>
    """
    
    js = """
    const chatMessages = document.getElementById('chatMessages');
    const queryInput = document.getElementById('queryInput');
    const sendBtn = document.getElementById('sendBtn');
    
    queryInput.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = Math.min(this.scrollHeight, 120) + 'px';
    });
    
    queryInput.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage(e);
        }
    });
    
    async function sendMessage(e) {
        e.preventDefault();
        const query = queryInput.value.trim();
        if (!query) {
            queryInput.focus();
            return false;
        }
        
        addMessage(query, 'user');
        queryInput.value = '';
        queryInput.style.height = 'auto';
        
        const loadingId = 'loading-' + Date.now();
        chatMessages.innerHTML += `<div class="chat-msg bot" id="${loadingId}">
            <div style="display:flex;align-items:center;gap:12px">
                <div class="spinner" style="width:20px;height:20px;margin:0;border-width:2px"></div>
                <span style="color:var(--muted)">Поиск по базе знаний...</span>
            </div>
        </div>`;
        chatMessages.scrollTop = chatMessages.scrollHeight;
        sendBtn.disabled = true;
        
        try {
            const resp = await fetch('/api/search', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({q: query})
            });
            const data = await resp.json();
            
            document.getElementById(loadingId).remove();
            
            if (data.error) {
                addMessage('Ошибка: ' + data.error, 'bot');
            } else {
                addMessageHtml(data.answer_html, data.meta);
            }
        } catch (err) {
            document.getElementById(loadingId).remove();
            addMessage('Ошибка соединения', 'bot');
        }
        
        sendBtn.disabled = false;
        queryInput.focus();
        return false;
    }
    
    function addMessage(text, type) {
        const div = document.createElement('div');
        div.className = 'chat-msg ' + type;
        div.textContent = text;
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
    
    function addMessageHtml(html, meta) {
        const div = document.createElement('div');
        div.className = 'chat-msg bot';
        div.innerHTML = html;
        if (meta) {
            const metaDiv = document.createElement('div');
            metaDiv.className = 'meta';
            metaDiv.textContent = meta;
            div.appendChild(metaDiv);
        }
        chatMessages.appendChild(div);
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }
    """
    return HTMLResponse(html_page("Поиск", body, js))


@app.post("/api/search")
async def api_search(request: Request):
    if not check_auth(request):
        return JSONResponse({"error": "Не авторизован"}, status_code=401)
    
    try:
        data = await request.json()
        query = data.get("q", "").strip()
    except:
        return JSONResponse({"error": "Неверный формат"}, status_code=400)
    
    if not query:
        return JSONResponse({"error": "Введите запрос"}, status_code=400)
    
    user = get_user(request)
    resp = graphrag.query(build_user_id(user or "demo"), query)
    log_event(user or "demo", query, "search_query", resp.answer)
    
    # Format the response with markdown-like styling
    formatted = format_llm_response(resp.answer)
    
    return JSONResponse({
        "answer_html": formatted,
        "meta": f"⏱ {resp.processing_time_ms}ms · {len(resp.context.entry_points)} точек · {len(resp.context.expanded_nodes)} узлов"
    })


# ========== VIEW ==========

@app.get("/view", response_class=HTMLResponse)
async def view_page(request: Request):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    user = get_user(request)
    uid = build_user_id(user or "demo")
    stats = linker.get_user_stats(uid)
    
    if stats["total_cards"] == 0:
        body = """
        <div class="container">
            <a href="/home" class="back-link">← На главную</a>
            <div class="page-header"><h2>💡 База знаний</h2></div>
            <div class="alert alert-error">📭 Граф пуст. Добавьте первую заметку!</div>
            <a href="/add" class="btn" style="text-decoration:none;text-align:center;display:block;margin-top:16px">➕ Добавить заметку</a>
        </div>
        """
        return HTMLResponse(html_page("База знаний", body))
    
    path = generate_graph_html_from_repo(linker.repository, uid, None)
    try:
        return HTMLResponse(Path(path).read_text(encoding="utf-8"))
    finally:
        Path(path).unlink(missing_ok=True)


# ========== DELETE (Card-based) ==========

@app.get("/delete", response_class=HTMLResponse)
async def delete_page(request: Request, msg: str = "", st: str = ""):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    alert = ""
    if msg:
        cls = "alert-success" if st == "ok" else "alert-error"
        alert = f'<div class="alert {cls}">{escape(msg)}</div>'
    
    body = f"""
    <div class="container">
        <a href="/home" class="back-link">← На главную</a>
        <div class="page-header"><h2>🗑 Удалить заметку</h2></div>
        
        {alert}
        
        <div class="msg-box">Опишите мысль, которую хотите найти и удалить.</div>
        
        <form action="/delete/search" method="post" data-enter-submit="true" data-loading-text="Поиск" data-loading-subtext="Ищем похожие заметки" onsubmit="return submitWithLoading(this, 'Поиск заметок', 'Ищем похожие мысли в базе знаний')">
            <div class="form-group"><textarea name="q" placeholder="Что удалить..." required></textarea></div>
            <button type="submit" class="btn">Найти</button>
        </form>
    </div>
    """
    return HTMLResponse(html_page("Удалить", body))


@app.post("/delete/search", response_class=HTMLResponse)
async def delete_search(request: Request, q: str = Form("")):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    user = get_user(request)
    uid = build_user_id(user or "demo")
    query = q.strip()
    
    if not query:
        return RedirectResponse("/delete?msg=Введите запрос&st=err", status_code=303)
    
    emb = embedding_model.embed_query(query)
    threshold = max(0.35, settings.linker_similarity_threshold)
    cands = linker.repository.vector_search(user_id=uid, query_embedding=emb, limit=5, similarity_threshold=threshold)
    
    if not cands:
        log_event(user or "demo", query, "delete_query", "Не найдено")
        return RedirectResponse("/delete?msg=Ничего не найдено. Попробуйте другую формулировку.&st=err", status_code=303)
    
    token = uuid.uuid4().hex
    cached = []
    cards_html = ""
    
    for i, (node, score) in enumerate(cands):
        cached.append({
            "zettel_id": node.zettel_id, 
            "luhmann_id": node.luhmann_id, 
            "content": node.content
        })
        preview = escape(node.content[:120]) + ("..." if len(node.content) > 120 else "")
        full_content = escape(node.content).replace("\n", "<br>")
        
        cards_html += f"""
        <div class="delete-card" onclick="showDeleteModal({i}, '{escape(node.luhmann_id)}', `{full_content}`)">
            <div class="card-header">
                <span class="card-id">[{escape(node.luhmann_id)}]</span>
                <span class="card-score">{score:.0%} совпадение</span>
            </div>
            <div class="card-preview">{preview}</div>
        </div>
        """
    
    DELETE_CACHE[token] = cached
    
    body = f"""
    <div class="container">
        <a href="/home" class="back-link">← На главную</a>
        <div class="page-header"><h2>🗑 Удалить заметку</h2></div>
        
        <div class="msg-box">Найдено {len(cached)} заметок. Нажмите на карточку, чтобы просмотреть и удалить.</div>
        
        <div class="delete-cards">{cards_html}</div>
        
        <form id="deleteForm" action="/delete/confirm" method="post" style="display:none">
            <input type="hidden" name="token" value="{token}">
            <input type="hidden" name="idx" id="deleteIdx">
        </form>
        
        <div id="deleteModal" class="modal-overlay">
            <div class="modal-box">
                <h3>🗑 Удалить эту мысль?</h3>
                <div class="card-id" id="modalCardId"></div>
                <div class="quote" id="modalQuote"></div>
                <p>Это действие нельзя отменить. Мысль и все связанные данные будут удалены.</p>
                <div class="modal-btns">
                    <button type="button" class="cancel" onclick="hideDeleteModal()">Отмена</button>
                    <button type="button" class="confirm" onclick="confirmDelete()">Удалить</button>
                </div>
            </div>
        </div>
        
        <a href="/delete" class="btn btn-outline" style="text-decoration:none;text-align:center;display:block;margin-top:20px">Новый поиск</a>
    </div>
    """
    
    js = """
    let pendingDeleteIdx = null;
    
    function showDeleteModal(idx, cardId, content) {
        pendingDeleteIdx = idx;
        document.getElementById('modalCardId').textContent = '[' + cardId + ']';
        document.getElementById('modalQuote').innerHTML = content;
        document.getElementById('deleteModal').classList.add('active');
    }
    
    function hideDeleteModal() {
        document.getElementById('deleteModal').classList.remove('active');
        pendingDeleteIdx = null;
    }
    
    function confirmDelete() {
        if (pendingDeleteIdx !== null) {
            document.getElementById('deleteIdx').value = pendingDeleteIdx + 1;
            showLoading('Удаление', 'Удаляем мысль из базы знаний');
            document.getElementById('deleteForm').submit();
        }
    }
    
    // Close modal on backdrop click
    document.getElementById('deleteModal').addEventListener('click', function(e) {
        if (e.target === this) hideDeleteModal();
    });
    
    // Close on Escape
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') hideDeleteModal();
    });
    """
    return HTMLResponse(html_page("Найденные заметки", body, js))


@app.post("/delete/confirm")
async def delete_confirm(request: Request, token: str = Form(""), idx: int = Form(0)):
    if not check_auth(request):
        return RedirectResponse("/login", status_code=303)
    
    user = get_user(request)
    uid = build_user_id(user or "demo")
    cands = DELETE_CACHE.pop(token, [])
    
    if not cands or idx < 1 or idx > len(cands):
        return RedirectResponse("/delete?msg=Данные устарели. Повторите поиск.&st=err", status_code=303)
    
    sel = cands[idx - 1]
    res = linker.repository.delete_zettel(uid, sel["zettel_id"])
    
    if not res:
        return RedirectResponse("/delete?msg=Не удалось удалить&st=err", status_code=303)
    
    stats = linker.get_user_stats(uid)
    msg = f"🗑 Удалено [{res['luhmann_id']}]\nУдалено: {res['deleted_count']} мысль(ей)\n📚 Осталось: {stats['total_cards']}"
    log_event(user or "demo", f"Удаление: {sel['content'][:50]}", "delete_query", msg)
    return RedirectResponse(f"/delete?msg={escape(msg)}&st=ok", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web_app:app", host="0.0.0.0", port=8008, reload=False)

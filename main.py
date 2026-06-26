import os
import asyncio
from html import escape
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from storage.postgres.db_connect import update_history_messages, create_database, create_tables
import telegram_bot.texts as texts
import telegram_bot.handlers.asr as asr
from telegram_bot.handlers.txt_reader import read_txt
from telegram_bot.handlers.pdf_reader import read_pdf
from config.settings import settings
from zettelkasten.atomizer import NoteAtomizer
from zettelkasten.linker import GraphLinker, LocalEmbeddingModel, LinkAction
from zettelkasten.graph_rag import GraphRAG
from zettelkasten.graph_visualizer import generate_graph_html_from_repo



load_dotenv()
# инициализация postgres: база и таблица истории сообщений
create_database()
create_tables()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# пайплайн zettelkasten / graphrag (изоляция данных по user_id)
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


def build_user_id(telegram_user_id: int) -> str:
    """
    Формирует user_id для изоляции графа в neo4j.
    Сейчас используется синтетический идентификатор tg_user_{id}.
    """
    return f"tg_user_{telegram_user_id}"


def save_user_note(user_id: str, text: str) -> tuple[bool, str]:
    """Атомизация текста и вставка карточек в граф конкретного пользователя."""
    # max_root_id нужен atomizer для корректной нумерации luhmann id внутри сообщения
    raw_cards = atomizer.atomize(
        text=text,
        current_db_max_root_id=linker.repository.get_max_root_id(user_id),
    )
    if isinstance(raw_cards, str):
        return False, f"Ошибка atomizer: {raw_cards}"

    results = linker.link_and_insert(user_id=user_id, new_cards=raw_cards)
    actions_count = {a: 0 for a in LinkAction}
    for result in results:
        actions_count[result.action] += 1

    stats = linker.get_user_stats(user_id)

    return True, (
        f"✅ Записано в граф знаний.\n"
        f"📚 Размер базы знаний: <b>{stats['total_cards']}</b> карточек"
    )


def _short_preview(text: str, max_len: int = 90) -> str:
    """Короткий превью-текст для сообщений в Telegram."""
    normalized = " ".join(text.split())
    if len(normalized) <= max_len:
        return normalized
    return normalized[: max_len - 1] + "…"

# состояния fsm: режим добавления, поиска, уточнения и удаления
class BotStates(StatesGroup):
    waiting_for_artifact = State()  # состояние ожидания добавления заметки   
    waiting_for_search = State()  # состояние ожидания поиска мыслей по запросу
    clarifying_text = State()  # состояние ожидания уточнения текста (добавление как заметку или поиск по контексту)
    waiting_for_delete_query = State()  # состояние ожидания поиска заметок на удаление

# клавиатуры главного меню и навигации
main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text='➕ Добавить новую заметку', callback_data='main_add')],
        [InlineKeyboardButton(text='🔍 Поиск мыслей по запросу', callback_data='main_search')],
        [InlineKeyboardButton(text='💡 Посмотреть базу знаний', callback_data='main_view')],
        [InlineKeyboardButton(text='🗑 Удалить заметку', callback_data='main_delete')]
    ]
)

cancel_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text='🔙 Назад на главное меню', callback_data='main_menu')]
    ]
)

# клавиатура отмена действия + назад на главное меню
cancel_and_undo_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text='🗑 Отменить/Удалить добавленную заметку', callback_data='undo_artifact_add')],
        [InlineKeyboardButton(text='🔙 Назад на главное меню', callback_data='main_menu')]
    ]
)

# клавиатура уточнения текста (добавление как заметка или поиск по контексту)
def get_clarify_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Добавить заметку", callback_data="action_add_artifact")],
            [InlineKeyboardButton(text="🔍 Найти мысли по контексту", callback_data="action_search_context")]
        ]
    )

# динамическая клавиатура выбора номера мысли для удаления (до 5 вариантов)
def get_delete_selection_kb(num_items: int = 5):
    # ограничиваем максимум до 5
    num_items = min(num_items, 5)
    
    # если ничего не найдено — только кнопка «назад»
    if num_items <= 0:
        return cancel_kb

    # кнопки с номерами 1..n
    number_buttons = []
    for i in range(1, num_items + 1):
        number_buttons.append(InlineKeyboardButton(text=str(i), callback_data=f'delete_item_{i}'))
    
    # первый ряд — номера, второй — «назад»
    inline_keyboard = [
        number_buttons,
        [InlineKeyboardButton(text='🔙 Назад на главное меню', callback_data='main_menu')]
    ]
    
    return InlineKeyboardMarkup(inline_keyboard=inline_keyboard)


# стартовый экран с приветствием и клавиатурой
@dp.message(Command('start'))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        texts.main_screen_text,
        parse_mode="HTML",
        reply_markup=main_kb
    )

# обработка нажатия на кнопку "Назад"
@dp.callback_query(F.data == 'main_menu')
async def cancel_action(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с кнопкой
    except TelegramBadRequest:
        pass
    await state.clear()
    await callback.message.answer(texts.main_screen_text, reply_markup=main_kb, parse_mode="HTML")


# обработка нажатия на кнопку "Посмотреть базу знаний"
@dp.callback_query(F.data == 'main_view')
async def view_knowledge_base(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с кнопками
    except TelegramBadRequest:
        pass
    
    # сбрасываем fsm: следующий текст попадёт в уточняющее меню
    await state.clear() 

    user_id = build_user_id(callback.from_user.id)
    stats = linker.get_user_stats(user_id)

    if stats["total_cards"] == 0:
        bot_answer = "📭 Ваш граф знаний пока пуст. Добавьте первую заметку!"
        await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
        return

    status_msg = await callback.message.answer(
        "⏳ <i>Генерирую интерактивный граф...</i>", parse_mode="HTML"
    )

    # html генерируется в отдельном потоке, чтобы не блокировать event loop бота
    try:
        loop = asyncio.get_running_loop()
        html_path = await loop.run_in_executor(
            None, generate_graph_html_from_repo, linker.repository, user_id, None
        )

        doc = FSInputFile(html_path, filename="knowledge_graph.html")
        bot_answer = (
            f"🧠 <b>Ваш граф знаний</b>\n"
            f"📚 Карточек: <b>{stats['total_cards']}</b>\n\n"
            "<i>Откройте файл в браузере — граф интерактивный: "
            "кликайте на узлы, перетаскивайте, масштабируйте, ищите.</i>"
        )
        await status_msg.delete()
        await callback.message.answer_document(
            doc, caption=bot_answer, parse_mode="HTML", reply_markup=cancel_kb
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Ошибка генерации графа: {escape(str(e))}",
            parse_mode="HTML", reply_markup=cancel_kb
        )
    finally:
        if "html_path" in locals() and os.path.exists(html_path):
            os.remove(html_path)

    update_history_messages(
        callback.from_user.id,
        callback.message.message_id,
        "Кнопка: Посмотреть базу",
        callback.message.date,
        'action',
        "Отправлен HTML-граф"
    )


# обработка нажатия на кнопку "➕ Добавить новую заметку"
@dp.callback_query(F.data == 'main_add')
async def btn_add_artifact(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с главным меню
    except TelegramBadRequest:
        pass
    await state.set_state(BotStates.waiting_for_artifact)
    await callback.message.answer(
        'Отправьте текстовое или голосовое сообщение или приложите файл (doc, pdf, txt), чтобы записать мысли в базу данных.\n\n<i>Можете отправлять их по очереди, я всё сохраню.</i>',
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

# обработка отмены (удаления) только что добавленной заметки
@dp.callback_query(F.data == 'undo_artifact_add')
async def undo_artifact_addition(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    # заглушка: полноценное удаление последней заметки пока не реализовано
    bot_answer = "🗑 <i>Последняя добавленная заметка была успешно удалена (отменена).</i>\n\nМожете продолжить добавление или вернуться в меню."
    
    # убираем кнопку отмены, чтобы нельзя было нажать дважды
    await callback.message.edit_text(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)


# обработка нажатия на кнопку "🔍 Поиск мыслей по запросу"
@dp.callback_query(F.data == 'main_search')
async def btn_search_context(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с главным меню
    except TelegramBadRequest:
        pass
    await state.set_state(BotStates.waiting_for_search)
    await callback.message.answer(
        'Введите ваш запрос для поиска по базе знаний:\n\n<i>Можете делать несколько запросов подряд.</i>',
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

# обработка текста в режиме поиска
@dp.message(BotStates.waiting_for_search, F.text)
async def process_search_query(message: Message, state: FSMContext):
    user_id = build_user_id(message.from_user.id)
    response = graphrag.query(user_id, message.text)
    bot_answer = escape(response.answer)
    
    # состояние поиска сохраняем — можно делать несколько запросов подряд
    await message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
    
    update_history_messages(
        message.from_user.id,
        message.message_id,
        message.text,
        message.date,
        'search_query',
        bot_answer
    )


# обработка нажатия на кнопку "🗑 Удалить заметку/знание"
@dp.callback_query(F.data == 'main_delete')
async def btn_delete_artifact(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с главным меню
    except TelegramBadRequest:
        pass
    await state.set_state(BotStates.waiting_for_delete_query)
    await callback.message.answer(
        'Напишите, какое знание вы хотите удалить, или опишите, о чем оно:\n\n<i>Я найду до 5 самых подходящих заметок для удаления.</i>',
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

# обработка текстового запроса: семантический поиск кандидатов на удаление
@dp.message(BotStates.waiting_for_delete_query, F.text)
async def process_delete_search_query(message: Message, state: FSMContext):
    user_id = build_user_id(message.from_user.id)
    query_embedding = embedding_model.embed_query(message.text)
    # порог не ниже 0.35, чтобы отсечь слабо релевантные варианты
    similarity_threshold = max(0.35, settings.linker_similarity_threshold)
    candidates = linker.repository.vector_search(
        user_id=user_id,
        query_embedding=query_embedding,
        limit=5,
        similarity_threshold=similarity_threshold,
    )

    if not candidates:
        bot_answer = (
            "Ничего подходящего не найдено.\n"
            "<i>Попробуйте более точную формулировку (ключевые слова из мысли).</i>"
        )
        await message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
        update_history_messages(
            message.from_user.id,
            message.message_id,
            message.text,
            message.date,
            'delete_query',
            bot_answer
        )
        return

    delete_candidates = []
    lines = ["<b>Найдено до 5 подходящих мыслей для удаления:</b>\n"]
    for i, (node, score) in enumerate(candidates, start=1):
        full_text = escape(node.content.strip())
        lines.append(
            f"{i}. <b>[{node.luhmann_id}]</b> {full_text}\n"
            f"   <i>релевантность: {score:.2f}</i>"
        )
        delete_candidates.append({
            "zettel_id": node.zettel_id,
            "luhmann_id": node.luhmann_id,
            "content": node.content,
            "score": score,
        })

    lines.append("\n👇 <b>Выберите номер мысли для удаления:</b>")
    bot_answer = "\n".join(lines)

    # сохраняем кандидатов в fsm, чтобы callback по номеру знал, что удалять
    await state.update_data(delete_candidates=delete_candidates)
    await message.answer(
        bot_answer,
        parse_mode="HTML",
        reply_markup=get_delete_selection_kb(len(delete_candidates))
    )

    update_history_messages(
        message.from_user.id,
        message.message_id,
        message.text,
        message.date,
        'delete_query',
        bot_answer
    )

# обработка нажатия на номер (1-5) для окончательного удаления
@dp.callback_query(F.data.startswith('delete_item_'))
async def confirm_item_deletion(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    item_num = callback.data.split('_')[-1] # получаем номер из callback_data
    
    try:
        await callback.message.delete() # удаляем сообщение с выбором списка
    except TelegramBadRequest:
        pass

    data = await state.get_data()
    candidates = data.get("delete_candidates", [])
    selected_idx = int(item_num) - 1

    if selected_idx < 0 or selected_idx >= len(candidates):
        bot_answer = (
            "⚠️ Список кандидатов устарел.\n"
            "<i>Отправьте новый запрос на удаление.</i>"
        )
        await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
        return

    selected = candidates[selected_idx]
    user_id = build_user_id(callback.from_user.id)
    # удаляется выбранная мысль и всё дочернее поддерево
    deletion_result = linker.repository.delete_zettel(user_id, selected["zettel_id"])

    if not deletion_result:
        bot_answer = (
            "⚠️ Не удалось удалить мысль: она уже удалена или не найдена."
        )
        await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
        return

    stats = linker.get_user_stats(user_id)
    deleted_preview = escape(_short_preview(deletion_result["content"], max_len=90))
    deleted_count = deletion_result["deleted_count"]
    if deleted_count > 1:
        deleted_text = f"Удалено мыслей: <b>{deleted_count}</b> (включая дочерние)."
    else:
        deleted_text = "Удалена 1 мысль."

    bot_answer = (
        f"🗑 <b>Мысль удалена:</b>\n"
        f"<i>[{deletion_result['luhmann_id']}]</i> {deleted_preview}\n\n"
        f"{deleted_text}\n"
        f"📚 Размер базы знаний: <b>{stats['total_cards']}</b> карточек"
    )

    await state.update_data(delete_candidates=[])
    await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)


# обработка входящего контента: текст, голос, файлы

# текст в режиме добавления заметки
@dp.message(BotStates.waiting_for_artifact, F.text)
async def artifact_text_message(message: Message, state: FSMContext):
    user_id = build_user_id(message.from_user.id)
    ok, bot_answer = save_user_note(user_id, message.text)
    if not ok:
        bot_answer = f"❌ {bot_answer}"
    
    # состояние не сбрасываем: пользователь может добавлять несколько заметок подряд
    await message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_and_undo_kb)
    
    update_history_messages(
        message.from_user.id,
        message.message_id,
        message.text,
        message.date,
        'text_artifact',
        bot_answer
    )

# документы обрабатываются всегда как добавление, независимо от fsm-состояния
@dp.message(F.document)
async def document_message(message: Message, bot: Bot, state: FSMContext):
    
    status_msg = await message.answer("⏳ <i>Выполняется обработка файла...</i>", parse_mode="HTML")
    
    doc_id = message.document.file_id
    doc_name = message.document.file_name
    
    # расширение определяет, какой ридер использовать
    _, ext = os.path.splitext(doc_name.lower())
    local_path = f"doc_{doc_id}{ext}"
    extracted_text = ""
    user_id = build_user_id(message.from_user.id)
    
    try:
        # поддерживаются только pdf и txt
        if ext not in ['.pdf', '.txt']:
            bot_answer = f'📋 Файл <b>{doc_name}</b> не поддерживается. <i>Поддерживаются только файлы .pdf и .txt.</i>'
            await status_msg.edit_text(bot_answer, parse_mode="HTML", reply_markup=main_kb)
            return

        # скачиваем файл во временный путь на диске
        file = await bot.get_file(doc_id)
        await bot.download_file(file.file_path, local_path)
        
        if ext == '.pdf':
            document_type = 'PDF'
            # pdf: ocr по страницам + таблицы в markdown
            pdf_data = read_pdf(local_path)
            extracted_text = "\n".join([f"--- Страница {page + 1} ---\n{text}" for page, text in pdf_data.items()])

        elif ext == '.txt':
            document_type = 'TXT'
            extracted_text = read_txt(local_path)
            
        if extracted_text.strip():
            ok, save_msg = save_user_note(user_id, extracted_text)
            if ok:
                bot_answer = f'📋 Файл <b>{doc_name}</b> обработан и добавлен в ваш граф знаний.\n\n{save_msg}'
            else:
                bot_answer = f'❌ Файл <b>{doc_name}</b> прочитан, но сохранить в граф не удалось: {save_msg}'
        else:
            bot_answer = f'⚠️ Файл <b>{doc_name}</b> обработан, но текст не извлечён.'
        
        current_state = await state.get_state()
        # в режиме добавления показываем кнопку отмены, иначе — главное меню
        markup = cancel_and_undo_kb if current_state == BotStates.waiting_for_artifact.state else main_kb
        
        await status_msg.edit_text(bot_answer, parse_mode="HTML", reply_markup=markup)
        
        if extracted_text:
            log_text = f"[Документ: {doc_name}]\nСодержимое:\n{extracted_text}"
        else:
            log_text = f"[Документ: {doc_name}]"
        
        # логируем в postgres
        update_history_messages(
            message.from_user.id,
            message.message_id,
            log_text,
            message.date,
            document_type,
            bot_answer
        )
        
    except Exception as e:
        bot_answer = f"Ошибка обработки файла: {e}"
        await status_msg.edit_text(bot_answer, parse_mode="HTML", reply_markup=main_kb)
        print(f"Ошибка обработки документа {doc_name}: {e}")

    finally:
        # удаляем временный файл
        if os.path.exists(local_path):
            os.remove(local_path)


# голосовые сообщения: ogg → wav → asr → pipeline добавления
@dp.message(F.voice)    
async def voice_message(message: Message, bot: Bot, state: FSMContext):
    
    msg = await message.answer("⏳ <i>Распознаю аудио...</i>", parse_mode="HTML")
    
    voice_file_id = message.voice.file_id
    
    # временные пути для ogg и wav
    ogg_path = f"voice_{voice_file_id}.ogg"
    wav_path = f"voice_{voice_file_id}.wav"
    user_id = build_user_id(message.from_user.id)
    
    try:
        # скачиваем голосовое сообщение в формате ogg
        file = await bot.get_file(voice_file_id)
        await bot.download_file(file.file_path, ogg_path)
        
        # ffmpeg конвертирует telegram ogg в wav для speech_recognition
        process = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', ogg_path, wav_path, '-y',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.communicate()
        
        # asr выполняется в thread pool, т.к. блокирующий вызов
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, asr.recognize_audio, wav_path)
        
        ok, save_msg = save_user_note(user_id, text)
        if ok:
            bot_answer = f'🎤 Голосовое сообщение обработано и записано в ваш граф:\n\n<i>"{text}"</i>\n\n{save_msg}'
        else:
            bot_answer = f'❌ Голос распознан, но не удалось сохранить в граф:\n\n<i>"{text}"</i>\n\n{save_msg}'
        log_text = f"[Голос]: {text}"
        
        current_state = await state.get_state()
        markup = cancel_and_undo_kb if current_state == BotStates.waiting_for_artifact.state else main_kb
        
        await msg.edit_text(bot_answer, parse_mode="HTML", reply_markup=markup)
        
        # логируем сообщение в историю
        update_history_messages(
            message.from_user.id,
            message.message_id,
            log_text,
            message.date,
            'voice',
            bot_answer
        )
                
    except Exception as e:
        bot_answer = f"Ошибка обработки аудио: {e}"
        await msg.edit_text(bot_answer, parse_mode="HTML", reply_markup=main_kb)
        print(f"Ошибка распознавания: {e}")
    
    finally:
        # удаляем временные ogg/wav
        if os.path.exists(ogg_path):
            os.remove(ogg_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)


# произвольный текст без активного режима: спрашиваем, добавить или искать
@dp.message(F.text)
async def unprompted_text_message(message: Message, state: FSMContext):
    # текст временно хранится в fsm до выбора действия в уточняющем меню
    await state.update_data(pending_text=message.text)
    await state.set_state(BotStates.clarifying_text)
    
    await message.answer(
        "Вы отправили текстовое сообщение. Что вы хотите с ним сделать?", 
        reply_markup=get_clarify_kb(),
        parse_mode="HTML"
    )

# обработка нажатия на кнопку "📥 Добавить заметку" (уточняющее меню)
@dp.callback_query(BotStates.clarifying_text, F.data == "action_add_artifact")
async def process_clarify_add(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с вопросом
    except TelegramBadRequest:
        pass
    
    data = await state.get_data()
    saved_text = data.get("pending_text", "")
    
    # переводим в режим добавления и сразу сохраняем отложенный текст
    await state.set_state(BotStates.waiting_for_artifact)
    
    user_id = build_user_id(callback.from_user.id)
    ok, save_msg = save_user_note(user_id, saved_text)
    if ok:
        bot_answer = f'{save_msg}\n\n<i>Можете отправлять заметки по очереди, я всё сохраню.</i>'
    else:
        bot_answer = f'❌ {save_msg}'
    await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_and_undo_kb)
    
    update_history_messages(
        callback.from_user.id,
        callback.message.message_id,
        saved_text,
        callback.message.date,
        'text_artifact',
        bot_answer
    )


# обработка нажатия на кнопку "🔍 Найти мысли по контексту" (уточняющее меню)
@dp.callback_query(BotStates.clarifying_text, F.data == "action_search_context")
async def process_clarify_search(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        await callback.message.delete() # удаляем сообщение с вопросом
    except TelegramBadRequest:
        pass
    
    data = await state.get_data()
    saved_text = data.get("pending_text", "")
    
    # переводим в режим поиска и выполняем graphrag по отложенному тексту
    await state.set_state(BotStates.waiting_for_search)
    
    user_id = build_user_id(callback.from_user.id)
    response = graphrag.query(user_id, saved_text)
    bot_answer = (
        f"{escape(response.answer)}\n\n"
        "<i>Можете отправлять запросы для поиска по очереди.</i>"
    )
    await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
    
    update_history_messages(
        callback.from_user.id,
        callback.message.message_id,
        saved_text,
        callback.message.date,
        'search_query',
        bot_answer
    )


async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
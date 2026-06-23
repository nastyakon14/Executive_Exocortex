import os
import asyncio
import random
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from telegram_bot.db.db_connect import update_history_messages, create_database, create_tables
import telegram_bot.texts as texts

load_dotenv()
# создаем базу данных и таблицы postgres
create_database()
create_tables()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

#  Состояния 
class BotStates(StatesGroup):
    waiting_for_artifact = State()  # состояние ожидания добавления артефакта   
    waiting_for_search = State()  # состояние ожидания поиска мыслей по запросу
    clarifying_text = State()  # состояние ожидания уточнения текста (добавление как артефакт или поиск по контексту)
    waiting_for_delete_query = State()  # состояние ожидания поиска артефактов на удаление

# Клавиатуры
main_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text='➕ Добавить новый артефакт', callback_data='main_add')],
        [InlineKeyboardButton(text='🔍 Поиск мыслей по запросу', callback_data='main_search')],
        [InlineKeyboardButton(text='💡 Посмотреть базу знаний', callback_data='main_view')],
        [InlineKeyboardButton(text='🗑 Удалить артефакт/знание', callback_data='main_delete')]
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
        [InlineKeyboardButton(text='🗑 Отменить/Удалить добавленный артефакт', callback_data='undo_artifact_add')],
        [InlineKeyboardButton(text='🔙 Назад на главное меню', callback_data='main_menu')]
    ]
)

# клавиатура уточнения текста (добавление как артефакт или поиск по контексту)
def get_clarify_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📥 Добавить как артефакт", callback_data="action_add_artifact")],
            [InlineKeyboardButton(text="🔍 Найти мысли по контексту", callback_data="action_search_context")]
        ]
    )

# динамическая клавиатура для выбора артефактов для удаления
# по дефолту обрабатывает до 5 элементов
def get_delete_selection_kb(num_items: int = 5):
    # Ограничиваем максимум до 5
    num_items = min(num_items, 5)
    
    # Если ничего не найдено, возвращаем только кнопку "Назад"
    if num_items <= 0:
        return cancel_kb

    # Генерируем кнопки с номерами динамически
    number_buttons = []
    for i in range(1, num_items + 1):
        number_buttons.append(InlineKeyboardButton(text=str(i), callback_data=f'delete_item_{i}'))
    
    # Собираем клавиатуру: первый ряд - номера, второй - кнопка "Назад"
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
    await callback.message.delete() # удаляем сообщение с кнопкой
    await state.clear()
    await callback.message.answer(texts.main_screen_text, reply_markup=main_kb, parse_mode="HTML")


# обработка нажатия на кнопку "Посмотреть базу знаний"
@dp.callback_query(F.data == 'main_view')
async def view_knowledge_base(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete() # удаляем сообщение с кнопками
    
    # Очищаем состояние, чтобы следующий отправленный текст вызвал уточняющее меню
    await state.clear() 

    # пользователь получит граф его текущих мыслей собранных между собой (пока что заглушка)
    bot_answer = "<i>[Заглушка] Здесь будет отображен персональный граф ваших знаний.</i>"
    
    # Отправляем ответ и прикрепляем кнопку "Назад" (cancel_kb)
    await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
    
    # логируем действие (БЕЗ await, так как psycopg2 синхронный)
    update_history_messages(callback.from_user.id, callback.message.message_id, "Кнопка: Посмотреть базу", callback.message.date, 'action', bot_answer)


# обработка нажатия на кнопку "➕ Добавить новый артефакт"
@dp.callback_query(F.data == 'main_add')
async def btn_add_artifact(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete() # удаляем сообщение с главным меню
    await state.set_state(BotStates.waiting_for_artifact)
    await callback.message.answer(
        'Отправьте текстовое или голосовое сообщение или приложите файл (doc, pdf, txt), чтобы записать мысли в базу данных.\n\n<i>Можете отправлять их по очереди, я всё сохраню.</i>',
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

# обработка отмены (удаления) только что добавленного артефакта
@dp.callback_query(F.data == 'undo_artifact_add')
async def undo_artifact_addition(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    # Здесь в будущем будет логика удаления конкретного ID из векторной БД
    bot_answer = "🗑 <i>Последний добавленный артефакт был успешно удален (отменен).</i>\n\nМожете продолжить добавление или вернуться в меню."
    
    # Изменяем текущее сообщение, чтобы убрать кнопку отмены (чтобы нельзя было нажать дважды)
    await callback.message.edit_text(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)


# обработка нажатия на кнопку "🔍 Поиск мыслей по запросу"
@dp.callback_query(F.data == 'main_search')
async def btn_search_context(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete() # удаляем сообщение с главным меню
    await state.set_state(BotStates.waiting_for_search)
    await callback.message.answer(
        'Введите ваш запрос для поиска по базе знаний:\n\n<i>Можете делать несколько запросов подряд.</i>',
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

# обработка текста в режиме поиска
@dp.message(BotStates.waiting_for_search, F.text)
async def process_search_query(message: Message, state: FSMContext):
    bot_answer = f"<i>[Заглушка RAG] Результаты поиска по запросу:</i> <b>{message.text}</b>"
    
    # Отвечаем,  не сбрасывая состояние, и вешаем кнопку "Назад"
    await message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)
    
    # логируем действие
    update_history_messages(message.from_user.id, message.message_id, message.text, message.date, 'search_query', bot_answer)


# обработка нажатия на кнопку "🗑 Удалить артефакт/знание"
@dp.callback_query(F.data == 'main_delete')
async def btn_delete_artifact(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete() # удаляем сообщение с главным меню
    await state.set_state(BotStates.waiting_for_delete_query)
    await callback.message.answer(
        'Напишите, какое знание вы хотите удалить, или опишите, о чем оно:\n\n<i>Я найду до 5 самых подходящих артефактов для удаления.</i>',
        reply_markup=cancel_kb,
        parse_mode="HTML"
    )

# ДИНАМИЧЕСКАЯ обработка текстового запроса для поиска кандидатов на удаление
@dp.message(BotStates.waiting_for_delete_query, F.text)
async def process_delete_search_query(message: Message, state: FSMContext):
    # Заглушка базы знаний для имитации поиска
    dummy_database = [
        "Знание про архитектуру бота",
        "Файл: document_v1.pdf",
        "Голосовая заметка про базу данных",
        "Заметка: купить молоко",
        "Текст: идея для стартапа"
    ]
    
    # Имитируем, что RAG-система нашла случайное количество результатов (от 1 до 5)
    found_count = random.randint(1, 5) 
    found_artifacts = dummy_database[:found_count] # Берем срез из массива
    
    # Динамически формируем текст ответа
    text_lines = [
        f"<i>[Заглушка RAG] Найдено {found_count} артефактов для удаления по запросу:</i> <b>{message.text}</b>\n"
    ]
    
    for i, artifact in enumerate(found_artifacts, start=1):
        text_lines.append(f"{i}. [{artifact}...]")
        
    text_lines.append("\n👇 <b>Выберите номер артефакта для удаления:</b>")
    bot_answer = "\n".join(text_lines)
    
    # Отвечаем, предоставляя динамическую клавиатуру
    await message.answer(bot_answer, parse_mode="HTML", reply_markup=get_delete_selection_kb(found_count))
    
    # логируем действие
    update_history_messages(message.from_user.id, message.message_id, message.text, message.date, 'delete_query', bot_answer)

# обработка нажатия на номер (1-5) для окончательного удаления
@dp.callback_query(F.data.startswith('delete_item_'))
async def confirm_item_deletion(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    item_num = callback.data.split('_')[-1] # получаем номер из callback_data
    
    await callback.message.delete() # удаляем сообщение с выбором списка
    
    bot_answer = f"🗑 <b>Артефакт №{item_num} успешно удален</b> из векторной базы знаний."
    
    # Сообщаем об успехе, оставляем возможность вернуться назад
    await callback.message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_kb)


# обработка контента: Текст, Голос, Файлы

# если прислали текст в режиме добавления артефакта
@dp.message(BotStates.waiting_for_artifact, F.text)
async def artifact_text_message(message: Message, state: FSMContext):
    bot_answer = '🌍 Информация успешно записана в базу знаний.'
    
    # Отвечаем, НЕ сбрасывая состояние, и вешаем клавиатуру с кнопкой отмены (cancel_and_undo_kb)
    await message.answer(bot_answer, parse_mode="HTML", reply_markup=cancel_and_undo_kb)
    
    # БЕЗ await
    update_history_messages(message.from_user.id, message.message_id, message.text, message.date, 'text_artifact', bot_answer)


# обработка файлов/документов (всегда добавление в артефакты, независимо от состояния)
@dp.message(F.document)
async def document_message(message: Message, state: FSMContext):
    doc_id = message.document.file_id
    doc_name = message.document.file_name
    bot_answer = f'📋 Файл <b>{doc_name}</b> обработан и добавлен в базу знаний.'
    
    current_state = await state.get_state()
    # Даем возможность отменить добавление, если мы в режиме добавления, иначе просто возвращаем в меню
    markup = cancel_and_undo_kb if current_state == BotStates.waiting_for_artifact.state else main_kb
    
    await message.answer(bot_answer, parse_mode="HTML", reply_markup=markup)
    
    # БЕЗ await
    update_history_messages(message.from_user.id, message.message_id, f"[Документ: {doc_name}]", message.date, 'document', bot_answer)


# обработка голосового сообщения (всегда добавление в артефакты, независимо от состояния)
@dp.message(F.voice)    
async def voice_message(message: Message, state: FSMContext):
    # сохранить содержимое голосового сообщения в векторную базу данных
    voice_file_id = message.voice.file_id
    voice_placeholder = f"[Голосовое сообщение, ID: {voice_file_id}]"
    
    bot_answer = '🎤 Голосовое сообщение обработано и записано в базу знаний.'
    
    current_state = await state.get_state()
    # Даем возможность отменить добавление
    markup = cancel_and_undo_kb if current_state == BotStates.waiting_for_artifact.state else main_kb

    await message.answer(bot_answer, parse_mode="HTML", reply_markup=markup)

    # логируем сообщение в историю сообщений (БЕЗ await)
    update_history_messages(message.from_user.id, message.message_id, voice_placeholder, message.date, 'voice', bot_answer)


# обработка текстового сообщения (без кнопок и состояний)
@dp.message(F.text)
async def unprompted_text_message(message: Message, state: FSMContext):
    # сохраняем текст пользователя во временную память машины состояний
    await state.update_data(pending_text=message.text)
    await state.set_state(BotStates.clarifying_text)
    
    await message.answer(
        "Вы отправили текстовое сообщение. Что вы хотите с ним сделать?", 
        reply_markup=get_clarify_kb(),
        parse_mode="HTML"
    )

# обработка нажатия на кнопку "📥 Добавить как артефакт" (уточняющее меню)
@dp.callback_query(BotStates.clarifying_text, F.data == "action_add_artifact")
async def process_clarify_add(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete() # удаляем сообщение с вопросом
    
    data = await state.get_data()
    saved_text = data.get("pending_text", "")
    
    # Переводим пользователя в режим добавления артефактов
    await state.set_state(BotStates.waiting_for_artifact)
    
    bot_answer = '🌍 Информация успешно записана в базу знаний.\n\n<i>Можете отправлять их по очереди, я всё сохраню.</i>'
    # Отправляем ответ и прикрепляем клавиатуру с кнопкой отмены (cancel_and_undo_kb)
    await callback.message.answer(f"<i>Ваш текст:</i> {saved_text}\n\n{bot_answer}", parse_mode="HTML", reply_markup=cancel_and_undo_kb)
    
    # логируем действие
    update_history_messages(callback.from_user.id, callback.message.message_id, saved_text, callback.message.date, 'text_artifact', bot_answer)


# обработка нажатия на кнопку "🔍 Найти мысли по контексту" (уточняющее меню)
@dp.callback_query(BotStates.clarifying_text, F.data == "action_search_context")
async def process_clarify_search(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.delete() # удаляем сообщение с вопросом
    
    data = await state.get_data()
    saved_text = data.get("pending_text", "")
    
    # Переводим пользователя в режим поиска
    await state.set_state(BotStates.waiting_for_search)
    
    bot_answer = f"<i>[Заглушка RAG] Результаты поиска мыслей:</i> <b>{saved_text}</b>\n\n<i>Можете отправлять запросы для поиска по очереди.</i>"
    # Отправляем ответ и прикрепляем кнопку "Назад"
    await callback.message.answer(f"<i>Ваш запрос:</i> {saved_text}\n\n{bot_answer}", parse_mode="HTML", reply_markup=cancel_kb)
    
    # логируем действие
    update_history_messages(callback.from_user.id, callback.message.message_id, saved_text, callback.message.date, 'search_query', bot_answer)


async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
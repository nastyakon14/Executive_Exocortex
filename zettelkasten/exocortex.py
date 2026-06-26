import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from config.settings import settings
from zettelkasten.atomizer import NoteAtomizer
from zettelkasten.linker import (
    GraphLinker,
    ZettelVectorDB,
    LocalEmbeddingModel,
    LinkAction,
)
load_dotenv()

# инициализация
embedding_model = LocalEmbeddingModel(
    model_name=settings.embedding_model_name,
            # "intfloat/multilingual-e5-small"  — быстрее, чуть хуже качество
            # "intfloat/multilingual-e5-large"  — медленнее, лучше качество
            # "ai-forever/sbert_large_nlu_ru"   — только русский, отличное качество
)

# атомайзер
atomizer = NoteAtomizer(
    model_name=settings.zettel_atomizer_model_name,
    temperature=settings.zettel_atomizer_temperature,
    system_prompt=settings.zettel_atomizer_prompt,
)

# векторная БД — передаём уже загруженную модель
# Режим (Docker/Persistent) определяется автоматически через фабрику на основе .env
db = ZettelVectorDB(
    embedding_model=embedding_model,  # передаём снаружи, не загружаем второй раз
)

# линкер
linker = GraphLinker(
    similarity_threshold=0.35,  # Порог сходства: < 0.35 = "не похоже"
    max_candidates=5,  # top_k 
)


def process_message(text: str, num: int) -> None:
    print(f"\n{'=' * 65}")
    print(f"📨 СООБЩЕНИЕ {num}:")
    print(f"   {text}")
    print(f"{'=' * 65}")

    #  1: atomizer
    raw_cards = atomizer.atomize(text=text, current_db_max_root_id=0)

    if isinstance(raw_cards, str):
        print(f"error atomizer: {raw_cards}")
        return

    print(f"\n⚗️  Атомайзер → {len(raw_cards)} карточек:")
    for c in raw_cards:
        marker = "🌱" if c.is_root_topic else "  🔗"
        print(f"   {marker} [{c.luhmann_id}] {c.content[:60]}...")

    # 2: linker (векторный поиск локально + LLM для решений)
    print(f"\n🔗 Линкер:")
    results = linker.link_and_insert(new_cards=raw_cards, db=db)

    #  3: итог
    actions_count = {a: 0 for a in LinkAction}
    for r in results:
        actions_count[r.action] += 1

    print(f"\n📊 Итог сообщения {num}:")
    icons = {"new_root": "🌱", "child_of": "🔗", "update_of": "🔄"}
    for action, count in actions_count.items():
        if count > 0:
            print(f"   {icons[action.value]} {action.value}: {count}")

    # Граф после обработки сообщения
    db.print_graph()



messages = [
    # 1. Первое сообщение: аудит b2b — всё новое, база пуста
    (
        "Нужно провести аудит b2b процессов до конца октября. "
        "Ответственный назначен Иванов Сергей. "
        "Фокус — выявить узкие места в воронке продаж и договорной базе."
    ),
    # 2. Совсем другая тема — поресурсный план для HR
    (
        "Составить поресурсный план аудитов для отдела кадров на Q4. "
        "Выделить бюджет 500 тысяч рублей. "
        "Куратор от HR — Петрова Анна."
    ),
    # 3. Возврат к b2b аудиту: факты изменились (UPDATE_OF) + новое (CHILD_OF)
    (
        "Важное обновление по аудиту b2b: срок сдвигается на середину ноября. "
        "Ответственным теперь назначен Козлов Дмитрий вместо Иванова. "
        "Дополнительно в рамках аудита нужно проверить интеграцию CRM с 1С."
    ),
    # 4. Конкретная задача по b2b аудиту (CHILD_OF)
    (
        "Для аудита b2b нужно запросить у отдела продаж выгрузку "
        "всех сделок за 12 месяцев в формате Excel."
    ),
]

if __name__ == "__main__":
    for i, msg in enumerate(messages, start=1):
        process_message(msg, i)

    print(f"\n✅ Готово. Карточек в базе: {db.total_count()}")
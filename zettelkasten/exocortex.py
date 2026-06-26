"""
exocortex.py — демонстрация пайплайна zettelkasten (neo4j + graphrag).
Изоляция по user_id: у каждого пользователя свой граф знаний.

Пайплайн:
1. atomizer: текст → атомарные карточки
2. linker: карточки → граф знаний (neo4j, фильтр по user_id)
3. graphrag: вопрос → ответ из графа пользователя
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from config.settings import settings
from zettelkasten.atomizer import NoteAtomizer
from zettelkasten.linker import (
    GraphLinker,
    LocalEmbeddingModel,
    LinkAction,
)

# инициализация компонентов пайплайна

embedding_model = LocalEmbeddingModel(
    model_name=settings.embedding_model_name,
)

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


# обработка одного сообщения для конкретного user_id

def process_message(user_id: str, text: str, num: int) -> None:
    """Обрабатывает одно сообщение для конкретного пользователя."""
    
    print(f"\n{'=' * 70}")
    print(f"📨 [{user_id}] СООБЩЕНИЕ {num}:")
    print(f"   {text}")
    print(f"{'=' * 70}")

    # шаг 1: atomizer — декомпозиция текста
    raw_cards = atomizer.atomize(
        text=text, 
        current_db_max_root_id=linker.repository.get_max_root_id(user_id)
    )

    if isinstance(raw_cards, str):
        print(f"❌ Ошибка Atomizer: {raw_cards}")
        return

    print(f"\n⚗️  Atomizer → {len(raw_cards)} карточек:")
    for c in raw_cards:
        marker = "🌱" if c.is_root_topic else "  🔗"
        print(f"   {marker} [{c.luhmann_id}] {c.content[:60]}...")

    # шаг 2: linker — встраивание в граф
    print(f"\n🔗 Linker:")
    results = linker.link_and_insert(user_id=user_id, new_cards=raw_cards)

    # Шаг 3: Статистика
    actions_count = {a: 0 for a in LinkAction}
    for r in results:
        actions_count[r.action] += 1

    print(f"\n📊 Итог:")
    icons = {"new_root": "🌱", "child_of": "🔗", "update_of": "🔄"}
    for action, count in actions_count.items():
        if count > 0:
            print(f"   {icons[action.value]} {action.value}: {count}")

    # Граф пользователя после обработки
    linker.print_graph(user_id)


# тестовые сообщения для cli-демо

DEMO_MESSAGES = [
    # 1. Первое сообщение: аудит b2b
    (
        "Нужно провести аудит b2b процессов до конца октября. "
        "Ответственный назначен Иванов Сергей. "
        "Фокус — выявить узкие места в воронке продаж и договорной базе."
    ),
    # 2. Другая тема — HR
    (
        "Составить поресурсный план аудитов для отдела кадров на Q4. "
        "Выделить бюджет 500 тысяч рублей. "
        "Куратор от HR — Петрова Анна."
    ),
    # 3. Обновление по b2b аудиту
    (
        "Важное обновление по аудиту b2b: срок сдвигается на середину ноября. "
        "Ответственным теперь назначен Козлов Дмитрий вместо Иванова. "
        "Дополнительно в рамках аудита нужно проверить интеграцию CRM с 1С."
    ),
    # 4. Конкретная задача по b2b
    (
        "Для аудита b2b нужно запросить у отдела продаж выгрузку "
        "всех сделок за 12 месяцев в формате Excel."
    ),
    # 5. Риск по подрядчику
    (
        "Есть риск срыва сроков аудита b2b из-за задержки данных от подрядчика Альфа-Аудит. "
        "Нужно запросить промежуточный статус у подрядчика до пятницы."
    ),
    # 6. Новый контур: финансы
    (
        "По финансовому блоку на следующий квартал нужно сократить операционные расходы на 8 процентов. "
        "Подготовить план оптимизации по трем статьям затрат."
    ),
]


# Расширенный multi-user набор для тестов изоляции.
# Синтетические user_id: как будто пришли из Telegram.
DEMO_USER_SCENARIOS = {
    "tg_user_10001": {
        "title": "Коммерческий директор",
        "messages": [
            "Закрыли сделку с Технопром на 4 миллиона рублей. Контакт со стороны клиента — Николай Власов.",
            "Нужно подготовить коммерческое предложение для СеверЭнерго до понедельника.",
            "Обновление: срок подготовки коммерческого предложения для СеверЭнерго переносится на среду.",
            "Риск: клиент СеверЭнерго может заморозить бюджет до конца месяца.",
        ],
        "questions": [
            "Какие сделки закрыты?",
            "Какие задачи по коммерческим предложениям сейчас актуальны?",
            "Какие риски по клиенту СеверЭнерго?",
        ],
    },
    "tg_user_10002": {
        "title": "HR-директор",
        "messages": [
            "Провести аттестацию сотрудников отдела маркетинга в июле.",
            "Куратором аттестации назначена Марина Орлова.",
            "Выделить бюджет 300 тысяч рублей на обучение новых менеджеров по продажам.",
            "Обновление: бюджет на обучение менеджеров увеличен до 420 тысяч рублей.",
        ],
        "questions": [
            "Кто отвечает за аттестацию?",
            "Какой бюджет на обучение менеджеров?",
            "Какие HR-задачи сейчас активны?",
        ],
    },
    "tg_user_10003": {
        "title": "Операционный директор",
        "messages": [
            "Нужно запустить пилот по автоматизации складской логистики на площадке Юг.",
            "Ответственным за пилот назначен Андрей Мельников.",
            "Срок запуска пилота — 15 августа.",
            "Риск: интеграция WMS может занять больше времени из-за доработки API.",
            "Дополнительно запросить у IT команды план интеграции WMS до вторника.",
        ],
        "questions": [
            "Кто отвечает за пилот логистики?",
            "Какие сроки запуска пилота?",
            "Какие риски и действия зафиксированы по WMS?",
        ],
    },
}


def demo_graphrag(user_id: str):
    """Демонстрация GraphRAG: задаём вопросы к базе знаний пользователя."""
    from zettelkasten.graph_rag import GraphRAG
    
    print("\n" + "=" * 70)
    print(f"🧠 GraphRAG Demo — поиск по графу знаний [{user_id}]")
    print("=" * 70)
    
    rag = GraphRAG(
        embedding_model=embedding_model,
        model_name=settings.graphrag_model_name,
        temperature=settings.graphrag_temperature,
        system_prompt=settings.graphrag_system_prompt,
        user_prompt_template=settings.graphrag_user_prompt_template,
        no_context_response=settings.graphrag_no_context_response,
        similarity_threshold=settings.graphrag_similarity_threshold,
    )
    
    questions = [
        "Кто отвечает за аудит b2b?",
        "Какие сроки по аудиту?",
        "Какой бюджет выделен на HR?",
        "Что нужно проверить в рамках аудита b2b?",
    ]
    
    for q in questions:
        print(f"\n❓ Вопрос: {q}")
        response = rag.query(user_id, q)
        print(f"📝 Ответ ({response.processing_time_ms}ms):")
        print(f"   {response.answer[:200]}..." if len(response.answer) > 200 else f"   {response.answer}")
        print(f"   (найдено {len(response.context.all_nodes)} карточек)")


def demo_multi_user():
    """Демонстрация изоляции: несколько разных пользователей."""
    
    print("\n" + "=" * 70)
    print("👥 DEMO: Несколько изолированных пользователей")
    print("=" * 70)

    from zettelkasten.graph_rag import GraphRAG
    rag = GraphRAG(
        embedding_model=embedding_model,
        model_name=settings.graphrag_model_name,
        temperature=settings.graphrag_temperature,
        system_prompt=settings.graphrag_system_prompt,
        user_prompt_template=settings.graphrag_user_prompt_template,
        no_context_response=settings.graphrag_no_context_response,
        similarity_threshold=settings.graphrag_similarity_threshold,
    )

    # Заполняем графы для каждого user_id отдельно.
    for user_id, scenario in DEMO_USER_SCENARIOS.items():
        print(f"\n📱 Пользователь: {user_id} ({scenario['title']})")
        for i, msg in enumerate(scenario["messages"], 1):
            process_message(user_id, msg, i)

    # Проверка изоляции и отдельных ответов
    print("\n" + "=" * 70)
    print("🔍 Проверка изоляции графов и пользовательских ответов")
    print("=" * 70)

    for user_id, scenario in DEMO_USER_SCENARIOS.items():
        stats = linker.get_user_stats(user_id)
        print(f"\n📊 {user_id} ({scenario['title']}): {stats['total_cards']} карточек")
        for q in scenario["questions"]:
            resp = rag.query(user_id, q)
            print(f"  ❓ {q}")
            print(f"    → {resp.answer[:150]}...")

    # Кросс-проверка одинакового вопроса разным пользователям
    print("\n" + "-" * 70)
    print("🔐 Кросс-проверка: один и тот же вопрос разным user_id")
    print("-" * 70)
    shared_question = "Какие риски сейчас зафиксированы?"
    for user_id in DEMO_USER_SCENARIOS:
        resp = rag.query(user_id, shared_question)
        print(f"{user_id} → {resp.answer[:120]}...")

    print("\n✅ Каждый user_id получает ответы только из своего графа.")


# точка входа cli

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Executive Exocortex Demo (Multi-User)")
    parser.add_argument("--user-id", default="demo_user_001", help="ID пользователя (по умолчанию: demo_user_001)")
    parser.add_argument("--multi-user", action="store_true", help="Расширенное демо с несколькими пользователями")
    parser.add_argument("--rag-only", action="store_true", help="Только GraphRAG (без вставки)")
    args = parser.parse_args()
    
    if args.multi_user:
        demo_multi_user()
    elif not args.rag_only:
        user_id = args.user_id
        print(f"\n📱 User ID: {user_id}")
        
        for i, msg in enumerate(DEMO_MESSAGES, start=1):
            process_message(user_id, msg, i)
        
        stats = linker.get_user_stats(user_id)
        print(f"\n✅ Готово. Карточек у {user_id}: {stats['total_cards']}")
        
        # демо graphrag по накопленному графу
        demo_graphrag(user_id)
    else:
        demo_graphrag(args.user_id)

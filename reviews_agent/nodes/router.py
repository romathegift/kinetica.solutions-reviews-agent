"""
Router: условное ребро графа после классификации.

Это не узел, а функция маршрутизации для add_conditional_edges. Она ничего
не пишет в состояние и никого не вызывает — только читает category
и возвращает имя следующего узла.

Маршруты (дизайн):
  spam                  -> END (без генерации: не тратим Sonnet и не шлём карточку)
  praise                -> generate (минуя Retrieve: контекст не нужен)
  complaint, question   -> retrieve -> generate
"""

from typing import Literal

from reviews_agent.state import ReviewState

# Имена узлов графа. Строки должны совпасть с ключами в add_node на шаге 7,
# иначе LangGraph упадёт при компиляции — и это хорошо: опечатка всплывёт
# на сборке, а не на прогоне.
NODE_RETRIEVE = "retrieve"
NODE_GENERATE = "generate"
NODE_SPAM_FILTER = "spam_filter"

Route = Literal["retrieve", "generate", "spam_filter"]


def route_by_category(state: ReviewState) -> Route:
    """
    Выбирает ветку по категории отзыва.

    Почему praise минует Retrieve: благодарность не требует фактов из базы
    знаний — ни policy, ни FAQ. Достаточно голоса бренда, который и так
    лежит в промпте генерации. Лишний поиск здесь стоил бы времени
    и тянул бы в контекст нерелевантные чанки, на которых модель может
    начать додумывать.

    Почему spam обрывается: отвечать рекламе незачем. Ветка кончается
    до Generate — ни вызова Sonnet, ни карточки оператору.
    """
    category = state["category"]

    if category == "spam":
        return NODE_SPAM_FILTER

    if category == "praise":
        return NODE_GENERATE

    # complaint и question — обоим нужен контекст из базы знаний.
    # Явный if вместо else: если в Category однажды добавится пятая категория,
    # она провалится в ошибку ниже, а не уедет молча в Retrieve.
    if category in ("complaint", "question"):
        return NODE_RETRIEVE

    # Недостижимо: classify валидирует category против того же набора.
    # Но если валидация однажды разъедется с роутером, лучше упасть здесь
    # с внятным сообщением, чем маршрутизировать наугад.
    raise ValueError(f"Router не знает категорию: {category!r}")
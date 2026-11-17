"""Система управления задачами (Todo-лист).

Единый файл с реализацией архитектуры из architecture.md.

Слои и направление зависимостей (стрелки только вниз):

    ConsoleUI   -> общается с пользователем (ввод/вывод)
       |
    TaskManager -> мозг приложения, единственный класс, который знают верхние слои
       |
    TaskList    -> контейнер задач (один список/проект)
       |
    Task        -> одна конкретная задача

    Storage     -> отдельная ветка: сохранение/загрузка, знает только dict

Запуск:
    python todo.py             # демонстрация работы системы
    python todo.py --console   # интерактивный консольный режим
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import sys
import tempfile
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional

DATA_FILE = "data.json"
DEFAULT_LIST_NAME = "Входящие"


# ---------------------------------------------------------------------------
# Исключения (exceptions.py)
# ---------------------------------------------------------------------------


class TodoError(Exception):
    """Базовое исключение приложения. UI ловит его и печатает понятный текст."""


class TaskNotFoundError(TodoError):
    """Задача с указанным id не найдена."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"Задача с id «{task_id}» не найдена")


class ListNotFoundError(TodoError):
    """Список с указанным id не найден."""

    def __init__(self, list_id: str) -> None:
        self.list_id = list_id
        super().__init__(f"Список с id «{list_id}» не найден")


class ValidationError(TodoError):
    """Некорректные данные: пустой title, дедлайн в прошлом, кривой статус и т.п."""


class StorageError(TodoError):
    """Ошибка чтения/записи хранилища."""


# ---------------------------------------------------------------------------
# Перечисления (models/enums.py)
# ---------------------------------------------------------------------------


class TaskStatus(Enum):
    """Статус задачи."""

    NEW = "new"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELED = "canceled"

    @property
    def label(self) -> str:
        """Русское название статуса для вывода."""
        return _STATUS_LABELS[self]

    @property
    def symbol(self) -> str:
        """Символ статуса для короткой строки задачи."""
        return _STATUS_SYMBOLS[self]


class Priority(Enum):
    """Приоритет задачи."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def label(self) -> str:
        """Русское название приоритета для вывода."""
        return _PRIORITY_LABELS[self]

    @property
    def weight(self) -> int:
        """Вес для сортировки: чем больше, тем важнее."""
        return _PRIORITY_WEIGHTS[self]


_STATUS_LABELS = {
    TaskStatus.NEW: "Новая",
    TaskStatus.IN_PROGRESS: "В работе",
    TaskStatus.DONE: "Готово",
    TaskStatus.CANCELED: "Отменена",
}

_STATUS_SYMBOLS = {
    TaskStatus.NEW: " ",
    TaskStatus.IN_PROGRESS: "~",
    TaskStatus.DONE: "x",
    TaskStatus.CANCELED: "-",
}

_PRIORITY_LABELS = {
    Priority.LOW: "Низкий",
    Priority.MEDIUM: "Средний",
    Priority.HIGH: "Высокий",
}

_PRIORITY_WEIGHTS = {Priority.LOW: 1, Priority.MEDIUM: 2, Priority.HIGH: 3}


# ---------------------------------------------------------------------------
# Утилиты (utils.py)
# ---------------------------------------------------------------------------

DATE_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d",
    "%d.%m.%Y",
)


def generate_id() -> str:
    """Возвращает новый уникальный id (UUID4)."""
    return str(uuid.uuid4())


def parse_date(value: str) -> datetime:
    """Разбирает дату из строки: 'ГГГГ-ММ-ДД[ ЧЧ:ММ]' или 'ДД.ММ.ГГГГ[ ЧЧ:ММ]'."""
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValidationError(
        f"Не удалось разобрать дату «{value}». Ожидается ГГГГ-ММ-ДД, "
        "ДД.ММ.ГГГГ или то же самое с временем ЧЧ:ММ"
    )


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Разбирает дату из ISO-строки (для десериализации). Пустое значение -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value))
    if result.tzinfo is not None:
        result = result.astimezone().replace(tzinfo=None)
    return result


def format_date(value: Optional[datetime]) -> str:
    """Форматирует дату для вывода; None -> прочерк."""
    return "—" if value is None else value.strftime("%d.%m.%Y %H:%M")


def format_task(task: Any) -> str:
    """Короткая строка задачи для вывода."""
    parts = [
        f"[{task.status.symbol}]",
        task.id[:4],
        f"«{task.title}»",
        f"({task.priority.label.lower()})",
    ]
    if task.deadline is not None:
        parts.append(f"до {format_date(task.deadline)}")
    if task.tags:
        parts.append(" ".join(f"#{tag}" for tag in task.tags))
    line = " ".join(parts)
    if task.completed_at is not None:
        line += f" — выполнено {format_date(task.completed_at)}"
    return line


def truncate(text: str, limit: int) -> str:
    """Обрезает длинный текст, добавляя многоточие."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_table(tasks: list) -> str:
    """Печатает задачи простой таблицей (без внешних библиотек)."""
    if not tasks:
        return "  (задач нет)"
    headers = ("ID", "СТАТУС", "ПРИОРИТЕТ", "ЗАДАЧА", "ДЕДЛАЙН")
    rows = [
        (
            task.id[:4],
            task.status.label,
            task.priority.label,
            truncate(task.title, 40),
            format_date(task.deadline),
        )
        for task in tasks
    ]
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]

    def render(row: tuple) -> str:
        return "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip()

    lines = [render(headers), "  ".join("-" * width for width in widths)]
    lines.extend(render(row) for row in rows)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Задача (models/task.py)
# ---------------------------------------------------------------------------


class Task:
    """Одна задача.

    Хранит данные и умеет менять только своё состояние. Ничего не знает
    о списках, менеджере и хранилище — вся валидация своих полей живёт здесь.
    """

    TITLE_MAX_LEN = 120
    UPDATE_FIELDS = ("title", "description", "priority", "tags", "deadline")

    def __init__(
        self,
        title: str,
        description: str = "",
        priority: Priority = Priority.MEDIUM,
        deadline: Optional[datetime] = None,
        tags: Optional[list[str]] = None,
        *,
        task_id: Optional[str] = None,
        status: TaskStatus = TaskStatus.NEW,
        created_at: Optional[datetime] = None,
        updated_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
    ) -> None:
        self._id = task_id or generate_id()
        self._title = self._validate_title(title)
        self._description = self._validate_description(description)
        self._priority = self._validate_priority(priority)
        self._status = self._validate_status(status)
        self._deadline = self._validate_deadline(deadline)
        now = datetime.now()
        self._created_at = created_at or now
        self._updated_at = updated_at or now
        self._completed_at = completed_at
        self._tags: list[str] = []
        for tag in tags or []:
            self._append_tag(tag)

    # --- свойства: id и created_at неизменяемы -----------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def updated_at(self) -> datetime:
        return self._updated_at

    @property
    def completed_at(self) -> Optional[datetime]:
        return self._completed_at

    @property
    def status(self) -> TaskStatus:
        return self._status

    @property
    def deadline(self) -> Optional[datetime]:
        return self._deadline

    @property
    def tags(self) -> list[str]:
        """Копия списка меток: наружу внутренний список не отдаём."""
        return list(self._tags)

    @property
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._title = self._validate_title(value)
        self._touch()

    @property
    def description(self) -> str:
        return self._description

    @description.setter
    def description(self, value: str) -> None:
        self._description = self._validate_description(value)
        self._touch()

    @property
    def priority(self) -> Priority:
        return self._priority

    @priority.setter
    def priority(self, value: Priority) -> None:
        self._priority = self._validate_priority(value)
        self._touch()

    # --- валидация ---------------------------------------------------------

    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("Название задачи должно быть строкой")
        title = value.strip()
        if not title:
            raise ValidationError("Название задачи не может быть пустым")
        if len(title) > cls.TITLE_MAX_LEN:
            raise ValidationError(
                f"Название задачи длиннее {cls.TITLE_MAX_LEN} символов"
            )
        return title

    @staticmethod
    def _validate_description(value: Optional[str]) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValidationError("Описание задачи должно быть строкой")
        return value.strip()

    @staticmethod
    def _validate_priority(value: Any) -> Priority:
        if isinstance(value, Priority):
            return value
        try:
            return Priority(str(value).strip().lower())
        except ValueError:
            allowed = ", ".join(item.value for item in Priority)
            raise ValidationError(
                f"Неизвестный приоритет «{value}». Допустимо: {allowed}"
            ) from None

    @staticmethod
    def _validate_status(value: Any) -> TaskStatus:
        if isinstance(value, TaskStatus):
            return value
        try:
            return TaskStatus(str(value).strip().lower())
        except ValueError:
            allowed = ", ".join(item.value for item in TaskStatus)
            raise ValidationError(
                f"Неизвестный статус «{value}». Допустимо: {allowed}"
            ) from None

    @staticmethod
    def _validate_deadline(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, str):
            if not value.strip():
                return None
            value = parse_date(value)
        if not isinstance(value, datetime):
            raise ValidationError("Дедлайн должен быть datetime или строкой с датой")
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        if value <= datetime.now():
            raise ValidationError("Дедлайн не может быть в прошлом")
        return value

    @staticmethod
    def _normalize_tag(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("Метка должна быть строкой")
        tag = value.strip().lstrip("#").strip().lower()
        if not tag:
            raise ValidationError("Метка не может быть пустой")
        return tag

    def _touch(self) -> None:
        """Обновляет отметку времени последнего изменения."""
        self._updated_at = datetime.now()


    # --- жизненный цикл: NEW -> IN_PROGRESS -> DONE -----------------------

    def start(self) -> "Task":
        """Переводит задачу NEW -> IN_PROGRESS."""
        if self._status is not TaskStatus.NEW:
            raise ValidationError(
                f"Начать можно только новую задачу (сейчас: {self._status.label})"
            )
        self._status = TaskStatus.IN_PROGRESS
        self._touch()
        return self

    def complete(self) -> "Task":
        """Переводит задачу в DONE и проставляет completed_at."""
        if self._status is not TaskStatus.IN_PROGRESS:
            raise ValidationError(
                f"Завершить можно только задачу в работе (сейчас: {self._status.label})"
            )
        self._status = TaskStatus.DONE
        self._completed_at = datetime.now()
        self._touch()
        return self

    def reopen(self) -> "Task":
        """Возвращает задачу из DONE/CANCELED в NEW и чистит completed_at."""
        if self._status not in (TaskStatus.DONE, TaskStatus.CANCELED):
            raise ValidationError(
                "Переоткрыть можно только завершённую или отменённую задачу "
                f"(сейчас: {self._status.label})"
            )
        self._status = TaskStatus.NEW
        self._completed_at = None
        self._touch()
        return self

    def cancel(self) -> "Task":
        """Переводит незавершённую задачу в CANCELED."""
        if self._status in (TaskStatus.DONE, TaskStatus.CANCELED):
            raise ValidationError(
                "Отменить можно только незавершённую задачу "
                f"(сейчас: {self._status.label})"
            )
        self._status = TaskStatus.CANCELED
        self._touch()
        return self

    # --- изменение полей ---------------------------------------------------

    def update(self, **fields: Any) -> "Task":
        """Меняет разрешённые поля и обновляет updated_at.

        Разрешено: title, description, priority, tags, deadline.
        Статус меняется только через start/complete/cancel/reopen.
        """
        if not fields:
            return self
        unknown = set(fields) - set(self.UPDATE_FIELDS)
        if unknown:
            raise ValidationError(
                "Нельзя изменить поля: "
                + ", ".join(sorted(unknown))
                + ". Разрешено: "
                + ", ".join(self.UPDATE_FIELDS)
            )
        if "deadline" in fields:
            self.set_deadline(fields.pop("deadline"))
        if "tags" in fields:
            tags = fields.pop("tags")
            if isinstance(tags, str):
                tags = [item for item in tags.split(",") if item.strip()]
            if not isinstance(tags, (list, tuple, set)):
                raise ValidationError("Теги должны быть списком строк")
            new_tags: list[str] = []
            for tag in tags:
                normalized = self._normalize_tag(tag)
                if normalized not in new_tags:
                    new_tags.append(normalized)
            self._tags = new_tags
        for name, value in fields.items():
            setattr(self, name, value)  # сеттеры свойств валидируют значения
        self._touch()
        return self

    def set_deadline(self, deadline: Optional[datetime]) -> "Task":
        """Ставит дедлайн (None — снять). Дедлайн в прошлом запрещён."""
        self._deadline = self._validate_deadline(deadline)
        self._touch()
        return self

    def is_overdue(self) -> bool:
        """Дедлайн прошёл, а задача не завершена и не отменена."""
        return (
            self._deadline is not None
            and self._status not in (TaskStatus.DONE, TaskStatus.CANCELED)
            and self._deadline < datetime.now()
        )

    # --- метки -------------------------------------------------------------

    def add_tag(self, tag: str) -> "Task":
        """Добавляет метку (дубликаты игнорируются)."""
        if self._append_tag(tag):
            self._touch()
        return self

    def remove_tag(self, tag: str) -> "Task":
        """Удаляет метку (если её нет — ничего не происходит)."""
        normalized = self._normalize_tag(tag)
        if normalized in self._tags:
            self._tags.remove(normalized)
            self._touch()
        return self

    def _append_tag(self, tag: str) -> bool:
        normalized = self._normalize_tag(tag)
        if normalized in self._tags:
            return False
        self._tags.append(normalized)
        return True


    # --- сериализация ------------------------------------------------------

    def to_dict(self) -> dict:
        """Сериализация в словарь: только простые типы, никаких объектов Task."""
        return {
            "id": self._id,
            "title": self._title,
            "description": self._description,
            "status": self._status.value,
            "priority": self._priority.value,
            "created_at": self._created_at.isoformat(timespec="seconds"),
            "updated_at": self._updated_at.isoformat(timespec="seconds"),
            "deadline": (
                self._deadline.isoformat(timespec="minutes")
                if self._deadline is not None
                else None
            ),
            "completed_at": (
                self._completed_at.isoformat(timespec="seconds")
                if self._completed_at is not None
                else None
            ),
            "tags": list(self._tags),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        """Восстанавливает задачу из словаря (обратная операция к to_dict)."""
        if not isinstance(data, dict):
            raise ValidationError("Данные задачи должны быть словарём")
        try:
            title = data["title"]
            status = cls._validate_status(data.get("status", TaskStatus.NEW.value))
            priority = cls._validate_priority(data.get("priority", Priority.MEDIUM.value))
            created_at = parse_iso_datetime(data.get("created_at")) or datetime.now()
            updated_at = parse_iso_datetime(data.get("updated_at")) or created_at
            completed_at = parse_iso_datetime(data.get("completed_at"))
            deadline = parse_iso_datetime(data.get("deadline"))
        except KeyError as exc:
            raise ValidationError(f"В данных задачи нет поля {exc}") from None
        except ValueError as exc:
            raise ValidationError(f"Некорректная дата в данных задачи: {exc}") from exc
        tags = data.get("tags") or []
        if not isinstance(tags, (list, tuple)):
            raise ValidationError("Теги в данных задачи должны быть списком")
        task = cls(
            title,
            description=data.get("description", ""),
            priority=priority,
            tags=list(tags),
            task_id=data.get("id") or generate_id(),
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
        )
        # Исторические данные могут содержать просроченный дедлайн: запрет
        # «дедлайн в прошлом» относится к пользовательскому вводу, а не к загрузке.
        task._deadline = deadline
        return task

    # --- служебное ---------------------------------------------------------

    def __str__(self) -> str:
        return format_task(self)

    def __repr__(self) -> str:
        return (
            f"Task(id={self._id!r}, title={self._title!r}, "
            f"status={self._status.value!r})"
        )

    def __eq__(self, other: object) -> bool:
        """Сравнение задач — по id, а не по названию."""
        if not isinstance(other, Task):
            return NotImplemented
        return self._id == other._id

    def __hash__(self) -> int:
        return hash(self._id)


# ---------------------------------------------------------------------------
# Список задач (models/task_list.py)
# ---------------------------------------------------------------------------


class TaskList:
    """Список задач (проект).

    Умеет искать и считать, но не умеет сохранять и не знает про консоль.
    Знает только Task.
    """

    SORT_KEYS = {
        "deadline": "deadline",
        "дедлайн": "deadline",
        "priority": "priority",
        "приоритет": "priority",
        "created_at": "created_at",
        "created": "created_at",
        "дата": "created_at",
    }

    def __init__(
        self,
        name: str,
        description: str = "",
        *,
        list_id: Optional[str] = None,
        tasks: Optional[list[Task]] = None,
        created_at: Optional[datetime] = None,
    ) -> None:
        self._id = list_id or generate_id()
        self._name = self._validate_name(name)
        self._description = (description or "").strip()
        self._created_at = created_at or datetime.now()
        self._tasks: list[Task] = []
        for task in tasks or []:
            self.add_task(task)

    # --- свойства ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def tasks(self) -> list[Task]:
        """Копия списка задач: наружу внутренний список не отдаём."""
        return list(self._tasks)

    # --- валидация ---------------------------------------------------------

    @staticmethod
    def _validate_name(value: str) -> str:
        if not isinstance(value, str):
            raise ValidationError("Название списка должно быть строкой")
        name = value.strip()
        if not name:
            raise ValidationError("Название списка не может быть пустым")
        return name

    # --- работа с задачами --------------------------------------------------

    def add_task(self, task: Task) -> Task:
        """Добавляет задачу, проверяя, что её id не дублируется."""
        if not isinstance(task, Task):
            raise ValidationError("В список можно добавлять только Task")
        if self.has_task(task.id):
            raise ValidationError(
                f"Задача с id «{task.id}» уже есть в списке «{self._name}»"
            )
        self._tasks.append(task)
        return task

    def remove_task(self, task_id: str) -> Task:
        """Удаляет задачу по id и возвращает её."""
        for index, task in enumerate(self._tasks):
            if task.id == task_id:
                return self._tasks.pop(index)
        raise TaskNotFoundError(task_id)

    def get_task(self, task_id: str) -> Task:
        """Возвращает задачу по id."""
        for task in self._tasks:
            if task.id == task_id:
                return task
        raise TaskNotFoundError(task_id)

    def has_task(self, task_id: str) -> bool:
        return any(task.id == task_id for task in self._tasks)

    # --- поиск, фильтр, сортировка -----------------------------------------

    def search(self, query: str) -> list[Task]:
        """Поиск по подстроке в title и description (без учёта регистра)."""
        needle = (query or "").strip().casefold()
        if not needle:
            return self.tasks
        return [
            task
            for task in self._tasks
            if needle in task.title.casefold() or needle in task.description.casefold()
        ]

    def filter(
        self,
        status: Optional[TaskStatus] = None,
        priority: Optional[Priority] = None,
        tag: Optional[str] = None,
    ) -> list[Task]:
        """Комбинируемый фильтр. Возвращает новый список, исходный не меняется."""
        status_value = Task._validate_status(status) if status is not None else None
        priority_value = Task._validate_priority(priority) if priority is not None else None
        tag_value = Task._normalize_tag(tag) if tag else None
        result = []
        for task in self._tasks:
            if status_value is not None and task.status is not status_value:
                continue
            if priority_value is not None and task.priority is not priority_value:
                continue
            if tag_value is not None and tag_value not in task.tags:
                continue
            result.append(task)
        return result

    def sort_by(self, key: str) -> list[Task]:
        """Сортировка по дедлайну / приоритету / дате создания.

        Возвращает новый список: порядок задач в контейнере не меняется.
        """
        normalized = self.SORT_KEYS.get((key or "").strip().lower())
        if normalized is None:
            raise ValidationError(
                f"Неизвестный ключ сортировки «{key}». "
                "Доступно: deadline, priority, created_at"
            )
        if normalized == "deadline":
            return sorted(
                self._tasks,
                key=lambda task: (
                    task.deadline is None,
                    task.deadline or datetime.max,
                ),
            )
        if normalized == "priority":
            return sorted(self._tasks, key=lambda task: task.priority.weight, reverse=True)
        return sorted(self._tasks, key=lambda task: task.created_at)


    # --- статистика и сериализация -----------------------------------------

    def get_overdue(self) -> list[Task]:
        """Задачи с прошедшим дедлайном, которые ещё не закрыты."""
        return [task for task in self._tasks if task.is_overdue()]

    def stats(self) -> dict:
        """Всего задач, разбивка по статусам и процент выполнения."""
        by_status = {status.value: 0 for status in TaskStatus}
        for task in self._tasks:
            by_status[task.status.value] += 1
        total = len(self._tasks)
        done = by_status[TaskStatus.DONE.value]
        return {
            "total": total,
            "by_status": by_status,
            "done": done,
            "done_percent": round(done * 100 / total, 1) if total else 0.0,
            "overdue": len(self.get_overdue()),
        }

    def rename(self, new_name: str) -> "TaskList":
        self._name = self._validate_name(new_name)
        return self

    def to_dict(self) -> dict:
        return {
            "id": self._id,
            "name": self._name,
            "description": self._description,
            "created_at": self._created_at.isoformat(timespec="seconds"),
            "tasks": [task.to_dict() for task in self._tasks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskList":
        if not isinstance(data, dict):
            raise ValidationError("Данные списка должны быть словарём")
        if "name" not in data:
            raise ValidationError("В данных списка нет поля 'name'")
        raw_tasks = data.get("tasks") or []
        if not isinstance(raw_tasks, list):
            raise ValidationError("Поле 'tasks' должно быть списком")
        try:
            created_at = parse_iso_datetime(data.get("created_at")) or datetime.now()
        except ValueError as exc:
            raise ValidationError(f"Некорректная дата создания списка: {exc}") from exc
        return cls(
            data["name"],
            description=data.get("description", ""),
            list_id=data.get("id") or generate_id(),
            tasks=[Task.from_dict(item) for item in raw_tasks],
            created_at=created_at,
        )

    # --- служебное ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._tasks)

    def __iter__(self):
        return iter(list(self._tasks))

    def __contains__(self, task: object) -> bool:
        return isinstance(task, Task) and self.has_task(task.id)

    def __str__(self) -> str:
        return f"{self._name} ({len(self)} задач)"

    def __repr__(self) -> str:
        return f"TaskList(id={self._id!r}, name={self._name!r}, tasks={len(self)})"


# ---------------------------------------------------------------------------
# Хранилище (storage/base.py, storage/memory_storage.py, storage/json_storage.py)
# ---------------------------------------------------------------------------


class Storage(ABC):
    """Интерфейс хранилища.

    Хранилище оперирует ТОЛЬКО словарями и не знает ни одного класса модели.
    """

    @abstractmethod
    def save(self, data: dict) -> None:
        """Сохраняет данные (dict)."""

    @abstractmethod
    def load(self) -> dict:
        """Возвращает данные (dict)."""


class InMemoryStorage(Storage):
    """Хранилище в памяти — для тестов, чтобы не мусорить файлами."""

    def __init__(self, data: Optional[dict] = None) -> None:
        self._data: dict = copy.deepcopy(data) if data else {"lists": []}

    def save(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise StorageError("Хранилище принимает только dict")
        self._data = copy.deepcopy(data)

    def load(self) -> dict:
        return copy.deepcopy(self._data)


class JsonFileStorage(Storage):
    """Хранилище в JSON-файле.

    Файла нет -> возвращается {"lists": []}. Битый JSON -> StorageError,
    при этом файл не перезаписывается. Перед перезаписью делается .bak-копия.
    """

    def __init__(self, file_path: str, encoding: str = "utf-8") -> None:
        if not isinstance(file_path, str) or not file_path.strip():
            raise StorageError("Не указан путь к файлу хранилища")
        self.file_path = file_path
        self.encoding = encoding

    def save(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise StorageError("Хранилище принимает только dict")
        if os.path.exists(self.file_path) and os.path.getsize(self.file_path) > 0:
            self._backup()
        tmp_path = self.file_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding=self.encoding) as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except OSError as exc:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise StorageError(
                f"Не удалось сохранить данные в «{self.file_path}»: {exc}"
            ) from exc

    def load(self) -> dict:
        if not os.path.exists(self.file_path):
            return {"lists": []}
        try:
            with open(self.file_path, "r", encoding=self.encoding) as file:
                raw = file.read()
        except OSError as exc:
            raise StorageError(
                f"Не удалось прочитать файл «{self.file_path}»: {exc}"
            ) from exc
        if not raw.strip():
            return {"lists": []}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"Файл «{self.file_path}» повреждён (строка {exc.lineno}). "
                "Данные не изменены: исправьте или удалите файл вручную."
            ) from exc
        if not isinstance(data, dict):
            raise StorageError(
                f"Файл «{self.file_path}» должен содержать JSON-объект"
            )
        return data

    def _backup(self) -> str:
        """Копия файла перед перезаписью. Возвращает путь к копии."""
        backup_path = self.file_path + ".bak"
        try:
            shutil.copy2(self.file_path, backup_path)
        except OSError as exc:
            raise StorageError(
                f"Не удалось создать резервную копию «{backup_path}»: {exc}"
            ) from exc
        return backup_path


# ---------------------------------------------------------------------------
# Менеджер (services/manager.py)
# ---------------------------------------------------------------------------


class TaskManager:
    """Мозг приложения: единственный класс, который знают верхние слои.

    После любой изменяющей операции сам вызывает save() — UI о сохранении
    не думает вообще.
    """

    def __init__(self, storage: Storage) -> None:
        if not isinstance(storage, Storage):
            raise TypeError("storage должен быть экземпляром Storage")
        self.storage = storage
        self.lists: dict[str, TaskList] = {}
        self.active_list_id: Optional[str] = None

    # --- списки ------------------------------------------------------------

    def _ensure_unique_name(self, name: str, exclude_id: Optional[str] = None) -> None:
        key = (name or "").strip().casefold()
        for task_list in self.lists.values():
            if task_list.id != exclude_id and task_list.name.casefold() == key:
                raise ValidationError(f"Список с именем «{name.strip()}» уже существует")

    def create_list(self, name: str, description: str = "") -> TaskList:
        """Создаёт список; имя должно быть уникальным в рамках приложения."""
        self._ensure_unique_name(name)
        task_list = TaskList(name, description)
        self.lists[task_list.id] = task_list
        if self.active_list_id is None:
            self.active_list_id = task_list.id
        self.save()
        return task_list

    def delete_list(self, list_id: str) -> TaskList:
        """Удаляет список вместе с его задачами.

        Последний список удалить нельзя. Подтверждение удаления — забота UI.
        """
        task_list = self.get_list(list_id)
        if len(self.lists) == 1:
            raise ValidationError(
                "Нельзя удалить последний список: сначала создайте другой"
            )
        del self.lists[list_id]
        if self.active_list_id == list_id:
            self.active_list_id = next(iter(self.lists))
        self.save()
        return task_list

    def rename_list(self, list_id: str, new_name: str) -> TaskList:
        task_list = self.get_list(list_id)
        self._ensure_unique_name(new_name, exclude_id=list_id)
        task_list.rename(new_name)
        self.save()
        return task_list

    def get_list(self, list_id: str) -> TaskList:
        try:
            return self.lists[list_id]
        except KeyError:
            raise ListNotFoundError(list_id) from None

    def get_all_lists(self) -> list[TaskList]:
        return list(self.lists.values())

    def set_active_list(self, list_id: str) -> TaskList:
        task_list = self.get_list(list_id)
        self.active_list_id = list_id
        self.save()
        return task_list

    @property
    def active_list(self) -> Optional[TaskList]:
        if self.active_list_id is None:
            return None
        return self.lists.get(self.active_list_id)

    # --- задачи ------------------------------------------------------------

    def _find_task(self, task_id: str) -> tuple[TaskList, Task]:
        """Ищет задачу по всем спискам (активный проверяется первым)."""
        ordered: list[TaskList] = []
        active = self.active_list
        if active is not None:
            ordered.append(active)
        ordered.extend(lst for lst in self.lists.values() if lst is not active)
        for task_list in ordered:
            if task_list.has_task(task_id):
                return task_list, task_list.get_task(task_id)
        raise TaskNotFoundError(task_id)

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: Priority = Priority.MEDIUM,
        deadline: Optional[datetime] = None,
        tags: Optional[list[str]] = None,
    ) -> Task:
        """Создаёт задачу в активном списке и сохраняет данные."""
        active = self.active_list
        if active is None:
            raise ValidationError("Нет активного списка: сначала создайте список")
        task = Task(
            title,
            description=description,
            priority=priority,
            deadline=deadline,
            tags=tags,
        )
        active.add_task(task)
        self.save()
        return task

    def edit_task(self, task_id: str, **fields: Any) -> Task:
        """Меняет разрешённые поля задачи (валидация — внутри Task.update)."""
        _, task = self._find_task(task_id)
        task.update(**fields)
        self.save()
        return task

    def change_status(self, task_id: str, new_status: TaskStatus) -> Task:
        """Меняет статус, вызывая соответствующий метод самой задачи."""
        _, task = self._find_task(task_id)
        try:
            status = (
                new_status
                if isinstance(new_status, TaskStatus)
                else TaskStatus(str(new_status).strip().lower())
            )
        except ValueError:
            allowed = ", ".join(item.value for item in TaskStatus)
            raise ValidationError(
                f"Неизвестный статус «{new_status}». Допустимо: {allowed}"
            ) from None
        if status is TaskStatus.NEW:
            task.reopen()
        elif status is TaskStatus.IN_PROGRESS:
            task.start()
        elif status is TaskStatus.DONE:
            task.complete()
        else:
            task.cancel()
        self.save()
        return task


    def delete_task(self, task_id: str) -> Task:
        task_list, task = self._find_task(task_id)
        task_list.remove_task(task_id)
        self.save()
        return task

    def move_task(self, task_id: str, target_list_id: str) -> Task:
        """Переносит задачу в другой список. Сама задача при этом не меняется."""
        source, task = self._find_task(task_id)
        target = self.get_list(target_list_id)
        if source is target:
            raise ValidationError("Задача уже находится в этом списке")
        target.add_task(task)
        source.remove_task(task_id)
        self.save()
        return task

    # --- сводные запросы ---------------------------------------------------

    def search_all(self, query: str) -> list[Task]:
        """Поиск по всем спискам."""
        result: list[Task] = []
        for task_list in self.lists.values():
            result.extend(task_list.search(query))
        return result

    def get_overdue_all(self) -> list[Task]:
        """Просроченные задачи по всем спискам."""
        result: list[Task] = []
        for task_list in self.lists.values():
            result.extend(task_list.get_overdue())
        return result

    def get_statistics(self) -> dict:
        """Сводка по всем спискам."""
        by_status = {status.value: 0 for status in TaskStatus}
        per_list: dict[str, dict] = {}
        total = 0
        for task_list in self.lists.values():
            stats = task_list.stats()
            per_list[task_list.name] = stats
            total += stats["total"]
            for status_value, count in stats["by_status"].items():
                by_status[status_value] += count
        done = by_status[TaskStatus.DONE.value]
        return {
            "lists_count": len(self.lists),
            "total": total,
            "by_status": by_status,
            "done_percent": round(done * 100 / total, 1) if total else 0.0,
            "overdue": len(self.get_overdue_all()),
            "per_list": per_list,
        }

    # --- сохранение и загрузка ---------------------------------------------

    def save(self) -> None:
        """Собирает to_dict() со всех списков и отдаёт в хранилище."""
        data = {
            "active_list_id": self.active_list_id,
            "lists": [task_list.to_dict() for task_list in self.lists.values()],
        }
        self.storage.save(data)

    def load(self) -> None:
        """Берёт dict из хранилища и восстанавливает объекты."""
        data = self.storage.load()
        if not isinstance(data, dict):
            raise StorageError("Хранилище вернуло не словарь")
        raw_lists = data.get("lists") or []
        if not isinstance(raw_lists, list):
            raise StorageError("Поле 'lists' в данных должно быть списком")
        restored: dict[str, TaskList] = {}
        for item in raw_lists:
            task_list = TaskList.from_dict(item)
            restored[task_list.id] = task_list
        self.lists = restored
        active_id = data.get("active_list_id")
        if isinstance(active_id, str) and active_id in restored:
            self.active_list_id = active_id
        else:
            self.active_list_id = next(iter(restored), None)

    def __str__(self) -> str:
        active = self.active_list.name if self.active_list else "—"
        return f"TaskManager(списков: {len(self.lists)}, активный: {active})"


# ---------------------------------------------------------------------------
# Консольный интерфейс (ui/console.py)
# ---------------------------------------------------------------------------


class ConsoleUI:
    """Общение с человеком.

    UI не меняет объекты напрямую: только зовёт методы менеджера
    и красиво печатает результат. Бизнес-логики в print нет.
    """

    def __init__(self, manager: TaskManager) -> None:
        self.manager = manager
        self.running = False
        self.commands: dict[str, Callable[[list[str]], None]] = {
            "help": self.cmd_help,
            "menu": self.cmd_help,
            "lists": self.cmd_lists,
            "use": self.cmd_use,
            "create-list": self.cmd_create_list,
            "rename-list": self.cmd_rename_list,
            "delete-list": self.cmd_delete_list,
            "add": self.cmd_add,
            "show": self.cmd_show,
            "search": self.cmd_search,
            "filter": self.cmd_filter,
            "start": self.cmd_start,
            "done": self.cmd_done,
            "cancel": self.cmd_cancel,
            "reopen": self.cmd_reopen,
            "edit": self.cmd_edit,
            "delete": self.cmd_delete_task,
            "move": self.cmd_move,
            "overdue": self.cmd_overdue,
            "stats": self.cmd_stats,
            "quit": self.cmd_quit,
            "exit": self.cmd_quit,
        }

    # --- главный цикл ------------------------------------------------------

    def run(self) -> None:
        """Показать меню -> прочитать ввод -> dispatch -> повторить."""
        self.running = True
        self.show_success("Todo-лист запущен. Введите help для списка команд.")
        while self.running:
            try:
                raw = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue
            try:
                parts = shlex.split(raw)
            except ValueError as exc:
                self.show_error(f"Не удалось разобрать команду: {exc}")
                continue
            self.handle_command(parts[0].lower(), parts[1:])
        print("До свидания!")

    def handle_command(self, cmd: str, args: list[str]) -> None:
        """Ищет обработчик в карте commands и вызывает его."""
        handler = self.commands.get(cmd)
        if handler is None:
            self.show_error(f"Неизвестная команда «{cmd}». Введите help.")
            return
        try:
            handler(args)
        except TodoError as exc:
            self.show_error(str(exc))
        except (EOFError, KeyboardInterrupt):
            print()
            self.running = False


    # --- вывод -------------------------------------------------------------

    def show_menu(self) -> None:
        print(
            "\nКоманды (id можно указывать по первым 4 символам):\n"
            "  help                                    — эта справка\n"
            "  lists                                   — показать списки\n"
            "  use <id|имя>                            — выбрать активный список\n"
            "  create-list <имя> [описание...]         — создать список\n"
            "  rename-list <id|имя> <новое имя>        — переименовать список\n"
            "  delete-list <id|имя>                    — удалить список (с подтверждением)\n"
            "  add [название задачи]                   — добавить задачу (без аргумента — диалог)\n"
            "  show                                    — задачи активного списка\n"
            "  search <текст>                          — поиск по всем спискам\n"
            "  filter <статус|-> <приоритет|-> <тег|-> — фильтр активного списка\n"
            "  start|done|cancel|reopen <id>           — сменить статус задачи\n"
            "  edit <id> поле=значение ...             — title/description/priority/deadline/tags\n"
            "  delete <id>                             — удалить задачу\n"
            "  move <id> <список>                      — перенести задачу\n"
            "  overdue                                 — просроченные задачи\n"
            "  stats                                   — статистика\n"
            "  quit                                    — выход\n"
            "Статусы: new, in_progress, done, canceled. Приоритеты: low, medium, high."
        )

    def show_lists(self) -> None:
        lists = self.manager.get_all_lists()
        if not lists:
            print("  (списков нет — создайте: create-list <имя>)")
            return
        print("  Списки:")
        for task_list in lists:
            marker = "*" if task_list.id == self.manager.active_list_id else " "
            description = f" — {task_list.description}" if task_list.description else ""
            print(
                f" {marker} {task_list.id[:4]}  {task_list.name:<20} "
                f"задач: {len(task_list):<3}{description}"
            )

    def show_tasks(self, tasks: list[Task]) -> None:
        print(format_table(tasks))

    def show_stats(self) -> None:
        stats = self.manager.get_statistics()
        print(
            f"Списков: {stats['lists_count']}, задач: {stats['total']}, "
            f"выполнено: {stats['done_percent']}%, просрочено: {stats['overdue']}"
        )
        for status in TaskStatus:
            print(f"  {status.label:<10}: {stats['by_status'][status.value]}")
        if stats["per_list"]:
            print("  По спискам:")
            for name, item in stats["per_list"].items():
                print(
                    f"    {name:<20} задач: {item['total']:<3} "
                    f"выполнено: {item['done_percent']}%  "
                    f"просрочено: {item['overdue']}"
                )

    def show_error(self, message: str) -> None:
        print(f"[ОШИБКА] {message}")

    def show_success(self, message: str) -> None:
        print(f"[OK] {message}")


    # --- ввод --------------------------------------------------------------

    def ask_task_data(self) -> dict:
        """Спрашивает у пользователя поля новой задачи."""
        title = input("Название: ").strip()
        description = input("Описание (Enter — пропустить): ").strip()
        priority_raw = input(
            "Приоритет (" + "/".join(item.value for item in Priority) + ") [medium]: "
        ).strip().lower()
        deadline_raw = input(
            "Дедлайн (ДД.ММ.ГГГГ [ЧЧ:ММ], Enter — без дедлайна): "
        ).strip()
        tags_raw = input("Теги через запятую (Enter — без тегов): ").strip()
        data: dict = {"title": title, "description": description}
        if priority_raw:
            try:
                data["priority"] = Priority(priority_raw)
            except ValueError:
                allowed = ", ".join(item.value for item in Priority)
                raise ValidationError(
                    f"Неизвестный приоритет «{priority_raw}». Допустимо: {allowed}"
                ) from None
        if deadline_raw:
            data["deadline"] = parse_date(deadline_raw)
        if tags_raw:
            data["tags"] = [item.strip() for item in tags_raw.split(",") if item.strip()]
        return data

    # --- разрешение ссылок (id по префиксу / имени) ------------------------

    def _resolve_list(self, ref: str) -> TaskList:
        key = (ref or "").strip()
        if not key:
            raise ValidationError("Укажите id или имя списка")
        lists = self.manager.get_all_lists()
        for task_list in lists:
            if task_list.id == key or task_list.name.casefold() == key.casefold():
                return task_list
        matches = [item for item in lists if item.id.startswith(key.lower())]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValidationError(f"Ссылка «{ref}» неоднозначна, укажите больше символов id")
        raise ListNotFoundError(key)

    def _resolve_task(self, ref: str) -> Task:
        key = (ref or "").strip().lower()
        if not key:
            raise ValidationError("Укажите id задачи")
        exact: list[Task] = []
        prefix: list[Task] = []
        for task_list in self.manager.get_all_lists():
            for task in task_list.tasks:
                if task.id == key:
                    exact.append(task)
                elif task.id.startswith(key):
                    prefix.append(task)
        if exact:
            return exact[0]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise ValidationError(f"Ссылка «{ref}» неоднозначна, укажите больше символов id")
        raise TaskNotFoundError(key)

    def _require_active_list(self) -> TaskList:
        active = self.manager.active_list
        if active is None:
            raise ValidationError("Нет активного списка. Создайте: create-list <имя>")
        return active


    # --- обработчики команд: списки ----------------------------------------

    def cmd_help(self, args: list[str]) -> None:
        self.show_menu()

    def cmd_lists(self, args: list[str]) -> None:
        self.show_lists()

    def cmd_use(self, args: list[str]) -> None:
        if not args:
            raise ValidationError("Формат: use <id|имя>")
        task_list = self.manager.set_active_list(self._resolve_list(args[0]).id)
        self.show_success(f"Активный список: «{task_list.name}»")
        self.show_lists()

    def cmd_create_list(self, args: list[str]) -> None:
        if not args:
            raise ValidationError("Формат: create-list <имя> [описание...]")
        task_list = self.manager.create_list(args[0], " ".join(args[1:]))
        self.show_success(f"Список «{task_list.name}» создан (id {task_list.id[:4]})")
        self.show_lists()

    def cmd_rename_list(self, args: list[str]) -> None:
        if len(args) < 2:
            raise ValidationError("Формат: rename-list <id|имя> <новое имя>")
        task_list = self._resolve_list(args[0])
        self.manager.rename_list(task_list.id, " ".join(args[1:]))
        self.show_success(f"Список переименован в «{task_list.name}»")

    def cmd_delete_list(self, args: list[str]) -> None:
        if not args:
            raise ValidationError("Формат: delete-list <id|имя>")
        task_list = self._resolve_list(args[0])
        answer = input(
            f"Удалить список «{task_list.name}» и {len(task_list)} задач(и)? "
            "Введите 'да' для подтверждения: "
        ).strip().lower()
        if answer not in ("да", "д", "yes", "y"):
            print("Удаление отменено.")
            return
        self.manager.delete_list(task_list.id)
        self.show_success(f"Список «{task_list.name}» удалён")
        self.show_lists()

    # --- обработчики команд: задачи ----------------------------------------

    def cmd_add(self, args: list[str]) -> None:
        self._require_active_list()
        if args:
            task = self.manager.create_task(title=" ".join(args))
        else:
            task = self.manager.create_task(**self.ask_task_data())
        self.show_success(f"Задача {task.id[:4]} создана в «{self.manager.active_list.name}»")
        self.show_tasks([task])

    def cmd_show(self, args: list[str]) -> None:
        task_list = self._require_active_list()
        print(f"Список «{task_list.name}»: {len(task_list)} задач(и)")
        self.show_tasks(task_list.tasks)

    def cmd_search(self, args: list[str]) -> None:
        query = " ".join(args).strip()
        if not query:
            raise ValidationError("Формат: search <текст>")
        tasks = self.manager.search_all(query)
        print(f"Найдено по запросу «{query}»: {len(tasks)}")
        self.show_tasks(tasks)

    def cmd_filter(self, args: list[str]) -> None:
        task_list = self._require_active_list()
        status: Optional[TaskStatus] = None
        priority: Optional[Priority] = None
        tag: Optional[str] = None
        try:
            if len(args) >= 1 and args[0] not in ("-", "*"):
                status = TaskStatus(args[0].strip().lower())
            if len(args) >= 2 and args[1] not in ("-", "*"):
                priority = Priority(args[1].strip().lower())
        except ValueError as exc:
            raise ValidationError(f"Неизвестное значение фильтра: {exc}") from exc
        if len(args) >= 3 and args[2] not in ("-", "*"):
            tag = args[2]
        tasks = task_list.filter(status=status, priority=priority, tag=tag)
        print(f"Под фильтр попало задач: {len(tasks)}")
        self.show_tasks(tasks)


    # --- обработчики команд: статусы --------------------------------------

    def _change_status(self, args: list[str], status: TaskStatus) -> None:
        if not args:
            raise ValidationError("Укажите id задачи")
        task = self._resolve_task(args[0])
        self.manager.change_status(task.id, status)
        self.show_success(f"Статус задачи {task.id[:4]} → {task.status.label}")
        self.show_tasks([task])

    def cmd_start(self, args: list[str]) -> None:
        self._change_status(args, TaskStatus.IN_PROGRESS)

    def cmd_done(self, args: list[str]) -> None:
        self._change_status(args, TaskStatus.DONE)

    def cmd_cancel(self, args: list[str]) -> None:
        self._change_status(args, TaskStatus.CANCELED)

    def cmd_reopen(self, args: list[str]) -> None:
        self._change_status(args, TaskStatus.NEW)

    # --- обработчики команд: изменение и удаление задач --------------------

    def cmd_edit(self, args: list[str]) -> None:
        if len(args) < 2:
            raise ValidationError(
                "Формат: edit <id> поле=значение ... "
                "(например: edit a1b2 priority=high title=\"Новое название\")"
            )
        task = self._resolve_task(args[0])
        fields: dict = {}
        for pair in args[1:]:
            if "=" not in pair:
                raise ValidationError(f"Ожидается поле=значение, получено: {pair}")
            field, value = pair.split("=", 1)
            field = field.strip().lower()
            if field == "priority":
                try:
                    fields[field] = Priority(value.strip().lower())
                except ValueError:
                    allowed = ", ".join(item.value for item in Priority)
                    raise ValidationError(
                        f"Неизвестный приоритет «{value}». Допустимо: {allowed}"
                    ) from None
            elif field == "deadline":
                fields[field] = None if value.strip() in ("", "-", "none") else parse_date(value)
            elif field == "tags":
                fields[field] = [item.strip() for item in value.split(",") if item.strip()]
            else:
                fields[field] = value
        self.manager.edit_task(task.id, **fields)
        self.show_success(f"Задача {task.id[:4]} изменена")
        self.show_tasks([task])

    def cmd_delete_task(self, args: list[str]) -> None:
        if not args:
            raise ValidationError("Укажите id задачи")
        task = self._resolve_task(args[0])
        self.manager.delete_task(task.id)
        self.show_success(f"Задача {task.id[:4]} удалена")

    def cmd_move(self, args: list[str]) -> None:
        if len(args) < 2:
            raise ValidationError("Формат: move <id задачи> <id|имя списка>")
        task = self._resolve_task(args[0])
        target = self._resolve_list(args[1])
        self.manager.move_task(task.id, target.id)
        self.show_success(f"Задача {task.id[:4]} перенесена в «{target.name}»")

    # --- обработчики команд: сводки и выход --------------------------------

    def cmd_overdue(self, args: list[str]) -> None:
        tasks = self.manager.get_overdue_all()
        if not tasks:
            self.show_success("Просроченных задач нет")
            return
        print(f"Просрочено задач: {len(tasks)}")
        self.show_tasks(tasks)

    def cmd_stats(self, args: list[str]) -> None:
        self.show_stats()

    def cmd_quit(self, args: list[str]) -> None:
        self.running = False


# ---------------------------------------------------------------------------
# Демонстрация работы системы
# ---------------------------------------------------------------------------


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def expect_error(label: str, action: Callable[[], Any]) -> None:
    """Показывает, что некорректное действие отклоняется с понятной ошибкой."""
    try:
        action()
    except TodoError as exc:
        print(f"  [ожидаемая ошибка] {label}: {exc}")
    else:
        print(f"  [ВНИМАНИЕ] {label}: ошибки не произошло")


def demo() -> None:
    """Создаёт объекты и прогоняет все ключевые сценарии системы."""
    section("1. Task: создание, валидация и жизненный цикл")
    milk = Task(
        "Купить молоко",
        description="2 литра, 3,2%",
        priority=Priority.HIGH,
        deadline=datetime.now() + timedelta(days=2),
        tags=["Дом", "покупки"],
    )
    print("Создана задача:", milk)
    print("id (UUID, не меняется):", milk.id)
    print("created_at == updated_at при создании:", milk.created_at == milk.updated_at)

    milk.add_tag("Срочно")
    milk.update(description="2 литра, 3,2%, безлактозное")
    print("После add_tag()/update():", milk)
    print("tags (нормализованы, без дублей):", milk.tags)

    milk.start()
    print("start():   ", milk)
    milk.complete()
    print("complete():", milk)
    print("completed_at:", format_date(milk.completed_at))
    milk.reopen()
    print("reopen():  ", milk)
    print("completed_at после reopen():", milk.completed_at)

    print("\nПроверки инвариантов Task:")
    expect_error("пустой title", lambda: Task("   "))
    expect_error(
        "дедлайн в прошлом",
        lambda: milk.set_deadline(datetime.now() - timedelta(days=1)),
    )
    expect_error("complete() из статуса NEW", milk.complete)
    expect_error(
        "неизвестный приоритет",
        lambda: milk.update(priority="очень-важно"),
    )

    historical = Task.from_dict(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "title": "Сдать квартальный отчёт",
            "description": "Цифры уже собраны, осталось оформить",
            "status": TaskStatus.IN_PROGRESS.value,
            "priority": Priority.HIGH.value,
            "created_at": (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds"),
            "updated_at": (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds"),
            "deadline": (datetime.now() - timedelta(days=1)).isoformat(timespec="minutes"),
            "completed_at": None,
            "tags": ["Работа", "отчёт"],
        }
    )
    print("\nЗадача, загруженная из словаря (дедлайн уже прошёл):")
    print(" ", historical)
    print("  is_overdue():", historical.is_overdue())
    print("  статус:", historical.status.label, "| приоритет:", historical.priority.label)


    section("2. TaskList: добавление, поиск, фильтр, сортировка, статистика")
    work = TaskList("Работа", "Рабочие задачи")
    work.add_task(Task("Позвонить клиенту", priority=Priority.LOW, tags=["звонки"]))
    work.add_task(
        Task(
            "Подготовить презентацию",
            priority=Priority.MEDIUM,
            deadline=datetime.now() + timedelta(days=3),
            tags=["Работа"],
        )
    )
    work.add_task(historical)
    print("Список:", work, "| len():", len(work))
    expect_error("повторное добавление той же задачи", lambda: work.add_task(historical))

    print("\nПоиск «отчёт»:", [task.title for task in work.search("отчёт")])
    print("Фильтр HIGH:", [task.title for task in work.filter(priority=Priority.HIGH)])
    print(
        "Фильтр IN_PROGRESS + тег #работа:",
        [task.title for task in work.filter(status=TaskStatus.IN_PROGRESS, tag="работа")],
    )
    print("Сортировка по дедлайну:", [task.title for task in work.sort_by("deadline")])
    print("Сортировка по приоритету:", [task.title for task in work.sort_by("priority")])
    print("Порядок в контейнере не изменился:", [task.title for task in work.tasks])
    print("Просроченные:", [task.title for task in work.get_overdue()])
    print("Статистика списка:", work.stats())
    expect_error("неизвестный ключ сортировки", lambda: work.sort_by("цвет"))

    section("3. Storage: сериализация в dict и обратно без потери данных")
    memory_storage = InMemoryStorage()
    memory_storage.save(work.to_dict())
    restored = TaskList.from_dict(memory_storage.load())
    before = work.get_task(historical.id)
    after = restored.get_task(historical.id)
    print("Восстановлен список:", restored)
    print("Количество задач совпадает:", len(restored) == len(work))
    print("id сохранился:", before.id == after.id)
    print(
        "статус и приоритет сохранились:",
        before.status is after.status,
        before.priority is after.priority,
    )
    print("дедлайн сохранился:", before.deadline == after.deadline)
    print("теги сохранились:", before.tags == after.tags)
    print("created_at сохранился:", before.created_at == after.created_at)
    print("\nСловарь задачи (то, что реально уходит в хранилище):")
    print(" ", after.to_dict())


    section("4. TaskManager: бизнес-сценарии без консоли")
    manager = TaskManager(InMemoryStorage())
    manager.load()
    print("После load() из пустого хранилища:", manager)

    inbox = manager.create_list("Входящие", "Список по умолчанию")
    study = manager.create_list("Учёба")
    home = manager.create_list("Дом")
    print("Созданы списки:", [str(item) for item in manager.get_all_lists()])
    print("Активный список:", manager.active_list.name)
    expect_error("дубликат имени списка", lambda: manager.create_list("учёба"))

    manager.set_active_list(study.id)
    homework = manager.create_task(
        "Сдать домашку по Python",
        priority=Priority.HIGH,
        deadline=datetime.now() + timedelta(days=5),
        tags=["python", "учёба"],
    )
    report = manager.create_task(
        "Квартальный отчёт",
        description="Свести цифры",
        priority=Priority.MEDIUM,
        deadline=datetime.now() + timedelta(days=2),
        tags=["работа"],
    )
    print("Активный список:", manager.active_list.name,
          "| задач в нём:", len(manager.get_list(study.id)))

    manager.change_status(homework.id, TaskStatus.IN_PROGRESS)
    manager.change_status(homework.id, TaskStatus.DONE)
    print("Домашка:", homework.status.label,
          "| completed_at:", format_date(homework.completed_at))
    manager.change_status(report.id, TaskStatus.IN_PROGRESS)
    expect_error(
        "неверный переход IN_PROGRESS -> reopen",
        lambda: manager.change_status(report.id, TaskStatus.NEW),
    )

    manager.move_task(report.id, home.id)
    print("Отчёт перенесён: «Учёба» —", len(manager.get_list(study.id)),
          "задач, «Дом» —", len(manager.get_list(home.id)))
    manager.edit_task(report.id, priority=Priority.HIGH, description="Цифры за квартал")
    print("После edit_task():", report)

    print("Поиск «Python» по всем спискам:",
          [task.title for task in manager.search_all("Python")])
    print("Задачи списка «Дом»:")
    ui = ConsoleUI(manager)
    ui.show_tasks(manager.get_list(home.id).tasks)

    print("\nConsoleUI ловит ошибку и не роняет приложение:")
    ui.handle_command("done", ["deadbeef"])

    archive_storage = InMemoryStorage(
        {
            "lists": [
                {
                    "id": "archive-list",
                    "name": "Архив",
                    "description": "проверка загрузки старых данных",
                    "created_at": (datetime.now() - timedelta(days=10)).isoformat(
                        timespec="seconds"
                    ),
                    "tasks": [
                        {
                            "id": "archive-task",
                            "title": "Просроченная задача из прошлого",
                            "description": "",
                            "status": TaskStatus.NEW.value,
                            "priority": Priority.LOW.value,
                            "created_at": (datetime.now() - timedelta(days=10)).isoformat(
                                timespec="seconds"
                            ),
                            "updated_at": (datetime.now() - timedelta(days=10)).isoformat(
                                timespec="seconds"
                            ),
                            "deadline": (datetime.now() - timedelta(days=2)).isoformat(
                                timespec="minutes"
                            ),
                            "completed_at": None,
                            "tags": ["архив"],
                        }
                    ],
                }
            ]
        }
    )
    archive_manager = TaskManager(archive_storage)
    archive_manager.load()
    print("\nПросроченные из загруженных данных:",
          [task.title for task in archive_manager.get_overdue_all()])

    print("\nСтатистика менеджера:")
    ui.show_stats()

    print("\nПроверка инварианта «последний список удалить нельзя»:")
    manager.delete_list(inbox.id)
    manager.delete_list(home.id)
    print("  осталось списков:", len(manager.get_all_lists()))
    expect_error("удаление последнего списка", lambda: manager.delete_list(study.id))


    section("5. JsonFileStorage: данные переживают перезапуск")
    with tempfile.TemporaryDirectory() as temp_dir:
        path = os.path.join(temp_dir, "data.json")
        first_manager = TaskManager(JsonFileStorage(path))
        first_manager.load()
        first_list = first_manager.create_list("Входящие", "список по умолчанию")
        saved_task = first_manager.create_task("Проверить сохранение", tags=["json"])
        print(
            "Файл создан:", os.path.basename(path),
            "| размер:", os.path.getsize(path), "байт",
        )

        second_manager = TaskManager(JsonFileStorage(path))
        second_manager.load()
        loaded_list = second_manager.get_all_lists()[0]
        loaded_task = loaded_list.tasks[0]
        print("После «перезапуска»:", second_manager)
        print("Задача восстановлена:", loaded_task)
        print("id совпадает:", loaded_task.id == saved_task.id)
        print(
            "активный список восстановлен:",
            second_manager.active_list is not None
            and second_manager.active_list.name == first_list.name,
        )

        second_manager.create_task("Вторая задача (при save появится .bak)")
        print("Файлы в каталоге:", sorted(os.listdir(temp_dir)))

    section("Готово")
    print("Демонстрация завершена. Интерактивный режим: python todo.py --console")


def main() -> None:
    """Точка входа интерактивного приложения: storage -> manager -> UI."""
    storage = JsonFileStorage(DATA_FILE)
    manager = TaskManager(storage)
    try:
        manager.load()
    except TodoError as exc:
        print(f"[ОШИБКА] Не удалось загрузить данные: {exc}")
        print("Приложение остановлено, чтобы не испортить файл данных.")
        return
    if not manager.get_all_lists():
        manager.create_list(DEFAULT_LIST_NAME, "Список по умолчанию")
    ConsoleUI(manager).run()


if __name__ == "__main__":
    if "--console" in sys.argv[1:]:
        main()
    else:
        demo()

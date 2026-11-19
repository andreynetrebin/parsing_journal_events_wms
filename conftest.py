"""Корневой conftest: гарантирует, что модули проекта видны тестам."""
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path, если его там ещё нет
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
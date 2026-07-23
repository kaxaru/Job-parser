"""HH.ru Job Market Analyzer.

Слои (MV*-разделение):
  config / models   — настройки и доменная модель (Model)
  data/             — доступ к данным: HH API, прокси, файловый кеш, разбор сырых JSON
  analyzer          — агрегация статистики поверх моделей (ViewModel)
  views/            — представления: консоль+CSV, Plotly-дашборд, HTML-лента
  hh.py (в корне)   — CLI-контроллер
"""

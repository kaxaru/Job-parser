import sys
from pathlib import Path

# делает пакет etl импортируемым при запуске pytest из dwh_demo
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Python runtime службы

Служба запускает установленный Python из `<Tracker>\.protected\runtime\python.exe` с ключом `-I`. Код worker находится в `<Tracker>\.protected\bin`; рабочий клон и корневая `.venv` используются только при разработке и тестировании.

В `vendor/` закреплены пакеты для Windows x64:

| Компонент | Версия | SHA-256 |
| --- | --- | --- |
| [CPython embeddable ZIP](https://www.python.org/downloads/release/python-3119/) | 3.11.9 | `009D6BF7E3B2DDCA3D784FA09F90FE54336D5B60F0E0F305C37F400BF83CFD3B` |
| [PyYAML wheel](https://pypi.org/project/PyYAML/6.0.3/) | 6.0.3, cp311 win_amd64 | `9F3BFB4965EB874431221A3FF3FDCDDC7E74E3B07799E0E84CA4A0F867D449BF` |

`scripts/Stage-PythonRuntime.ps1` принимает пустой каталог staging, сверяет контрольные суммы пакетов, копирует и повторно проверяет их в staging, распаковывает runtime и PyYAML, затем проверяет `python.exe -I` и импорт `yaml`. Установщик должен вызвать этот сценарий до остановки действующей службы и переносить в защищённый runtime только успешно проверенный результат.

Проверка в репозитории: `pwsh -NoProfile -File tests/test-stage-python-runtime.ps1`. Её файлы остаются в `.runtime/tests/stage-python-runtime/`.

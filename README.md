# Task Folder MCP

Служба Windows регистрирует проекты, выделяет ключи задач и подготавливает защищённые каталоги для артефактов task-flow. MCP-адаптер работает под учётной записью агента, служба — под отдельной учётной записью `TaskFolderMcpSvc`. Выбор TRAIN и расчёт снапшотов в сервис не входят.

## Переключение существующей службы на рабочие каталоги

Служба `TaskFolderMcp` переключена на рабочий корень и работает под учётной записью `TaskFolderMcpSvc`. Рабочие пути: обычные задачи — `D:\Projects\Tracker\tasks`, защищённые каталоги — `C:\ProgramData\TaskFolderMcp\protected`. Установщик копирует существующий реестр `projects.json` в защищённый корень и не меняет ACL рабочего каталога задач. Проверки транспорта, AIDEV-58 и отказа агенту в прямом доступе к защищённому корню прошли 29.09.2026. Обновлённый плагин Codex ещё требует выпуска и установки.

В **обычном PowerShell под `VASIL\Vasil`** подготовьте основной `C:\Users\Vasil\.env\task-folder-mcp.json` (повторный запуск безопасен). Если там находится прежняя тестовая конфигурация, bootstrap сохранит её как `task-folder-mcp.test.json` и переключит основной JSON на рабочие пути:

```powershell
pwsh -NoProfile -File 'D:\Projects\Git\ai-reps\task-folder-mcp\scripts\Bootstrap-TaskFolderMcp.ps1'
```

В **PowerShell с правами администратора под `VASIL\Vasil`** переключите службу:

```powershell
pwsh -NoProfile -File 'D:\Projects\Git\ai-reps\task-folder-mcp\scripts\Install-TaskFolderMcp.ps1' -ConfigPath 'C:\Users\Vasil\.env\task-folder-mcp.json'
```

После переключения выполните под обычной учётной записью `tests/test-installed-transport.ps1` и `tests/test-installed-production.ps1`. Вторая проверка читает рабочий реестр и разрешает AIDEV-58 через MCP, затем убеждается, что обычная учётная запись не может читать реестр напрямую или писать в защищённый корень. Для проверки повторной установки под администратором используйте `tests/test-installer-idempotent.ps1 -ConfigPath 'C:\Users\Vasil\.env\task-folder-mcp.json'`. Тест создания `TFMTEST` к рабочему реестру не применим.

Подключение клиентов Codex требует обновлённого плагина `task-folder-workflow` из `ai-marketplace`: его MCP запускает установленный `mcp-adapter.js`. Выпуск плагина 0.4.0 отложен по решению пользователя. Пока плагин не обновлён, старый MCP продолжает использовать прежнюю конфигурацию и не обращается к службе.

## Тестовая установка на рабочей машине

Для проверки используются отдельные тестовые каталоги на рабочей машине:

| Назначение | Путь |
| --- | --- |
| Папки задач с правом записи у агента | `D:\Projects\Tracker\task-folder-mcp-tests\tasks` |
| Защищённые каталоги и реестр проектов | `C:\ProgramData\TaskFolderMcp\protected-test` |
| Установленные файлы службы и MCP-адаптера | `C:\Program Files\TaskFolderMcp` |

Для отдельной тестовой установки выполните в **обычном окне PowerShell под `VASIL\Vasil`**:

```powershell
pwsh -NoProfile -File 'D:\Projects\Git\ai-reps\task-folder-mcp\scripts\Bootstrap-TaskFolderMcp.ps1' -Target Test
```

Проверьте файл `C:\Users\Vasil\.env\task-folder-mcp.test.json`. Затем выполните в **PowerShell с правами администратора под той же учётной записью**:

```powershell
pwsh -NoProfile -File 'D:\Projects\Git\ai-reps\task-folder-mcp\scripts\Install-TaskFolderMcp.ps1' -ConfigPath 'C:\Users\Vasil\.env\task-folder-mcp.test.json'
```

При первом создании службы установщик запросит пароль учётной записи `TaskFolderMcpSvc`. Он также назначит ей право Windows «Вход в качестве службы» (`SeServiceLogonRight`). Проект `TFMTEST` создаётся только в отдельном тестовом реестре. Если `TaskFolderMcpSvc` входит в локальную группу администраторов, установщик удалит её из этой группы и выдаст необходимые права на защищённое хранилище напрямую. Пароль в JSON не записывается.

Оба шага идемпотентны. Повторный bootstrap не меняет JSON, даже если файл создан до появления сервисной учётной записи. Повторный запуск установщика сохраняет `projects.json` и существующие папки задач. Если код и конфигурация не изменились, работающая служба не перезапускается; при обновлении кода или конфигурации служба перезапускается.

Команда запуска MCP-адаптера от имени агента:

```text
C:\Program Files\nodejs\node.exe C:\Program Files\TaskFolderMcp\mcp-adapter.js
```

## Проверки

```powershell
node tests/test-folder-worker.js
node tests/test-mcp-adapter.js
pwsh -NoProfile -File tests/test-bootstrap-idempotent.ps1
```

Вторая проверка использует локальный именованный канал и может потребовать запуска вне песочницы команд Codex. После установки службы `tests/test-installed-transport.ps1` проверяет серию вызовов через установленный адаптер, `tests/test-installed-folders.ps1` — параллельное создание задач и конфликт `request_id`, а `tests/test-installed-boundary.ps1` — запрет прямого доступа к защищённому каталогу под обычным токеном. В PowerShell с правами администратора можно выполнить `tests/test-service-logon-right.ps1` и `tests/test-installer-idempotent.ps1`. Последний тест проверяет, что повторная установка сохраняет PID службы и реестр проектов. Результаты проверки границы прав Windows приведены в [docs/windows-boundary-probe.md](docs/windows-boundary-probe.md).

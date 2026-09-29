# AIDEV-60: права родителя Tracker

Проверка 30.09.2026 выполнена скриптом `tests/probe-tracker-parent-acl.ps1` на одноразовых каталогах в `.runtime/tests/A60-02/`. Машинные протоколы: `setup-result.json`, `first-attempt.json`, `unsafe-parent-result.json` в той же папке.

| Объект | Владелец до проверки | Наследование | Права `VASIL\Vasil` до проверки |
| --- | --- | --- | --- |
| `unsafe-parent` | `VASIL\Vasil` | отключено | ReadAndExecute |
| `unsafe-parent/Tracker-delete` | `VASIL\TaskFolderMcpSvc` | отключено | ReadAndExecute |
| `unsafe-parent/Tracker-rename` | `VASIL\TaskFolderMcpSvc` | отключено | ReadAndExecute |

Остальные явные разрешения каждого каталога: FullControl для SYSTEM, Administrators и `TaskFolderMcpSvc`. Подготовка выполнена в повышенном сеансе. Операции выполнены процессом `VASIL\Vasil`, SID `S-1-5-21-272856448-2182906433-824961816-1007`, без повышения, medium integrity `S-1-16-8192`.

| Операция под обычным токеном | Результат |
| --- | --- |
| Выдать себе FullControl на `unsafe-parent` через `icacls` | выполнено |
| Удалить `Tracker-delete` | выполнено |
| Переименовать `Tracker-rename` | выполнено |

Первый вызов `Set-Acl` запросил отсутствующую `SeSecurityPrivilege` и не поменял DACL; `first-attempt.json` сохранён для диагностики инструмента. Повтор через `icacls` изменил именно DACL родителя. Итог: сервисное владение Tracker и отключённое наследование не защищают каталог, если агент владеет его родителем.

Рабочий путь `D:\Projects\Tracker` небезопасен при текущей границе: и `D:\Projects`, и `D:\Projects\Tracker` принадлежат `VASIL\Vasil`; на них также есть Authenticated Users: Modify. Установщик должен отклонять такой путь. ACL существующего `D:\Projects` ради Tracker не менялся.

## Выбранный защищённый родитель

Пользователь выбрал `D:\TaskTrackers\Vasil`. 30.09.2026 `tests/probe-tracker-safe-root.ps1` создал защищённый родитель `D:\TaskTrackers` и два временных дочерних каталога. Протоколы сохранены в `.runtime/tests/A60-02/safe-setup.json` и `safe-agent.json`.

| Объект | Владелец | Наследование | Права `VASIL\Vasil` |
| --- | --- | --- | --- |
| `D:\` | SYSTEM | отключено | Authenticated Users: Modify, без ChangePermissions и DeleteSubdirectoriesAndFiles |
| `D:\TaskTrackers` | Administrators | отключено | ReadAndExecute |
| оба `_AIDEV60-probe-*` | `VASIL\TaskFolderMcpSvc` | отключено | ReadAndExecute |

Проба под тем же неповышенным `VASIL\Vasil`, medium integrity `S-1-16-8192`, дала три отказа: изменение ACL `D:\TaskTrackers` через `icacls`, удаление и переименование дочерних каталогов. Оба дочерних каталога сохранились. Таким образом, выбранная цепочка родителей выдержала проверенные операции для этого токена. Каталог `D:\TaskTrackers\Vasil` ещё не создан; его владелец и ACL будут назначены при установке отдельной службы. Временные `_AIDEV60-probe-*` будут удалены на следующем администраторском шаге командой `tests/probe-tracker-safe-root.ps1 -Mode Cleanup`; защищённый родитель останется.

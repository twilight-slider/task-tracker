# Task Tracker

Персональная Windows-служба для папок задач и снимков проектов. Служба выполняет Python-код из защищённого каталога Tracker; плагины Codex подключаются к её локальному MCP endpoint.

## Требования

- **Windows и права администратора.** Bootstrap и установка службы запускаются в повышенной PowerShell; целевой пользователь и сервисная учётная запись должны быть разными.
- **PowerShell 7 и Git.** `pwsh` выполняет сценарии, `git` проверяет клон и получает опубликованный marketplace. Репозиторий task-tracker должен быть на чистой `main`, совпадающей с локальной `origin/main`.
- **Python и пакеты.** Установленные Windows launcher `py.exe`, требуемая версия Python с `venv` и `pip`, доступный источник пакетов для установки PyYAML. Установщик создаёт отдельный защищённый venv; Python сам не скачивает.
- **Компилятор .NET Framework 4.** `csc.exe` из Windows нужен при сборке или пересборке ядра службы.
- **Node.js, Codex Desktop и CLI `codex`.** Node запускает MCP-клиенты двух плагинов; Codex устанавливает их из опубликованного Git marketplace.

## Настраиваемое окружение

- **`-TargetUser`.** Обязательное имя пользователя Windows, для которого работает Tracker, например `DOMAIN\User`. Bootstrap читает профиль именно этого пользователя, даже если администратор вошёл под другой учётной записью.
- **`<профиль TargetUser>\.env\env.txt`.** Обычный UTF-8 файл, создаваемый пользователем до первого запуска. В нём нужна ровно одна запись `TRACKER_FOLDER=<абсолютный выделенный каталог Tracker>` и одна `GIT_FOLDER=<существующий абсолютный корень проектов, например C:\Git>`.
- **`GIT_FOLDER`.** При первой установке без `-SnapshotRoots` становится разрешённым корнем снимков. При переустановке без параметра сохраняются корни из установленного `service.json`.
- **`TRACKER_MCP_TOKEN`.** Вручную не задаётся: первая установка создаёт его в пользовательском `env.txt` и защищённом Tracker; переустановка сверяет и сохраняет обе копии.
- **[`.venv/python_version.txt`](.venv/python_version.txt) и [requirements.txt](requirements.txt).** Версионируемые настройки Python и пакетов. Сейчас задан `python3.11` и `PyYAML==6.0.3`; при отсутствии этой версии установка останавливается без fallback.
- **`-SnapshotRoots`, `-ServiceAccountName`, `-TrustAuthenticatedUsers`.** Необязательные параметры bootstrap: первый явно задаёт один или несколько корней снимков, второй — имя сервисной учётной записи, третий допускает широкий доступ к родительскому каталогу Tracker только при сознательном доверии этим пользователям.

## Краткая схема установки

Из корня репозитория в **повышенной PowerShell**:

```powershell
pwsh -NoProfile -File .\bootstrap.ps1 -TargetUser 'DOMAIN\User'
pwsh -NoProfile -File '<TRACKER_FOLDER>\.protected\TaskTracker-AdminWrapper.ps1'
```

Корневой `bootstrap.ps1` — раппер: имя целевого пользователя задаётся один раз в аргументе `-TargetUser`, редактировать файл не нужно. Он проверяет Git, готовит защищённый корень и создаёт в нём `bootstrap.json` и административный wrapper. При первой установке `GIT_FOLDER` задаёт корень снимков; при повторной сохраняются уже установленные корни. Явный `-SnapshotRoots` переопределяет их. Вторая команда устанавливает или обновляет службу и плагины. Если администратор сознательно доверяет пользователям с правом записи в родительский каталог Tracker, добавьте `-TrustAuthenticatedUsers` к первой команде. Владельца существующего Tracker скрипты не меняют.

После установки полностью перезапустите Codex обычным способом. Плагины сами читают `TRACKER_MCP_TOKEN` из пользовательского `env.txt`; отдельный файл запуска Codex не нужен.

Подробности: [административный bootstrap](docs/admin-bootstrap.md), [установка v3](docs/install-v3.md). [Старая схема Node.js](docs/install-new-machine.md) сохранена для переходного периода.

## Схема прав

- **Родительский каталог Tracker** принадлежит Administrators или SYSTEM. По умолчанию другие учётные записи не должны иметь права создавать, удалять или менять ACL в нём. `-TrustAuthenticatedUsers` осознанно допускает широкий доступ к родителю и ослабляет эту границу.
- **Корень Tracker** при первом запуске создаётся с владельцем Administrators: администраторы и SYSTEM получают полный доступ, целевой пользователь — чтение и выполнение. Владельца существующего Tracker bootstrap не меняет; целевой пользователь не может быть его владельцем или иметь право удаления и изменения ACL.
- **`.protected`** не наследует ACL. Полный доступ имеют администраторы, SYSTEM и сервисная учётная запись; целевой пользователь не получает права менять служебный код, runtime, конфигурацию и защищённую копию токена. Сервисная учётная запись должна отличаться от целевого пользователя и не быть администратором.
- **Папки задач** создаёт служба. Целевой пользователь получает чтение каталогов, а в разрешённых каталогах содержимого — создание файлов; ему не выдаётся удаление и переименование защищённых каталогов. Вторая копия MCP-токена хранится в пользовательском `.env\env.txt`; доступ к этому файлу следует ограничить владельцем профиля.

## Что проверяют скрипты

1. Корневой bootstrap требует повышенную PowerShell, чистую ветку `main` и равенство `HEAD` локальному `origin/main`; сетевой `fetch` он не делает.
2. По `-TargetUser` определяется SID и профиль. Проверяются обычный файл `.env\env.txt`, ровно один абсолютный `TRACKER_FOLDER`; при первой установке без `-SnapshotRoots` — ровно один существующий абсолютный `GIT_FOLDER`. Корни снимков должны существовать, быть обычными каталогами и не пересекаться с Tracker.
3. Подготовка проверяет владельца и ACL родителя, корня и `.protected`; существующего владельца не меняет. Bootstrap сверяет SID установленной службы, сервисную учётную запись и защищённые файлы, затем создаёт `bootstrap.json` и wrapper.
4. Защищённый wrapper повторяет проверки Git, SID, путей, владельцев и ACL. Установщик проверяет Python заданной версии, `venv`, `pip` и PyYAML, компилятор C#, опубликованный marketplace, обе копии MCP-токена и привязку существующей службы до переключения файлов.
5. После установки проверяются статус службы `Running/Automatic`, авторизованный MCP-запрос и наличие инструментов обоих плагинов. При сбое установки выполняется откат; незавершённый откат помечается `RECOVERY_INCOMPLETE`.

## Ошибки установки и исправление

Здесь перечислены предусмотренные скриптами группы отказов. Начала сообщений приведены без машинных путей и SID; сообщения с одинаковым исправлением объединены. Системные ошибки Windows, Git, `pip` и Codex сохраняют свой исходный текст.

| Сообщение или признак | Что исправить |
| --- | --- |
| `Run this TaskTracker step from an elevated PowerShell window` | Запустить PowerShell от администратора. |
| `Specify -TargetUser`; `Cannot resolve TargetUser`; `TargetUser profile is invalid` | Передать существующее имя `DOMAIN\User`; убедиться, что у пользователя создан профиль. |
| `TargetUser env file is missing`; `env file must be plain` | Создать обычный `<профиль>\.env\env.txt`, не ссылку, доступный для чтения администратору. |
| `exactly one TRACKER_FOLDER`; `TRACKER_FOLDER must be an absolute path`; `cannot be a volume root` | Оставить одну запись `TRACKER_FOLDER=<абсолютный выделенный каталог>`; не указывать корень диска. |
| `exactly one GIT_FOLDER`; `GIT_FOLDER must be an absolute path` | При первой установке добавить одну запись `GIT_FOLDER=<существующий абсолютный корень проектов>`, например `C:\Git`, либо явно передать `-SnapshotRoots`. |
| `SnapshotRoots cannot be empty`; `At least one snapshot root is required`; `Bootstrap JSON must specify at least one snapshot root` | Задать `GIT_FOLDER` или непустой `-SnapshotRoots`; при повторной установке проверить сохранённые корни в `service.json`. |
| `Directory path must be absolute`; `contains a file or reparse point`; `Snapshot root must be separate from Tracker storage` | Указать существующий обычный каталог абсолютным путём; не использовать ссылку и не пересекать его с Tracker. |
| `Git check failed`; `checkout root differs`; `checkout must be on main`; `HEAD differs from local origin/main` | Проверить Git и правильный клон, перейти на `main`, синхронизировать его с локальным `origin/main`. |
| `checkout has staged, unstaged or untracked changes`; `Checkout commit differs from bootstrap JSON` | Закоммитить или убрать изменения; после нового commit повторно запустить корневой bootstrap. |
| `Choose a dedicated Tracker path`; `TRACKER_FOLDER is inside a user/system directory` | Выбрать отдельный каталог Tracker под выделенным родителем, вне профиля пользователя, системных каталогов и корня диска. |
| `Tracker parent is not administrator-owned`; `parent permits untrusted delete/ACL rights` | Подготовить родитель с владельцем Administrators/SYSTEM и безопасным ACL. `-TrustAuthenticatedUsers` использовать только при сознательном доверии всем пишущим пользователям. |
| `Existing Tracker is owned by TargetUser`; `TargetUser can delete or alter ACL`; `Untrusted Tracker boundary owner`; `Untrusted account can alter Tracker boundary` | Исправить права или выбрать другой корень с администраторским/сервисным владельцем. Bootstrap владельца существующего корня не меняет. |
| `Protected Tracker area is absent`; `ACL still inherits permissions`; `Protected Tracker owner must be`; `untrusted account can write protected` | Запустить подготовку корня или восстановить защищённый ACL `.protected` под администратором; затем повторить bootstrap. |
| `Protected ... not a plain file`; `untrusted owner`; `permits untrusted write`; `reparse point` | Убрать подменённый файл/ссылку, восстановить обычный файл и доверенного владельца/ACL; повторить bootstrap из чистого клона. |
| `Cannot resolve installed service account`; `Installed service account SID differs`; `Service account SID differs`; `Service account must differ from TargetUser`; `must not be an administrator` | Сверить фактическую учётную запись службы и `service.json`; использовать отдельную неадминистративную сервисную учётную запись. |
| `ServiceAccountName must be ... at most 20 characters` | Передать допустимое имя локальной учётной записи длиной не более 20 символов. |
| `TargetUser env/profile differs from bootstrap JSON`; `TargetUser settings differ`; `Existing service is bound to another Tracker root` | Вернуть согласованные пути и пользователя либо повторить bootstrap; не переключать службу на другой корень вручную. |
| `ConfigPath must be absolute`; `Administrative bootstrap JSON v3 is required`; `Bootstrap JSON must identify the published ... Git source`; `Protected bootstrap identity is invalid`; `JSON has an unsupported schema`; `protected paths differ`; `service fields are inconsistent`; `Rebuild must use ... JSON` | Использовать абсолютный путь к созданному bootstrap JSON v3 и штатный опубликованный marketplace; не править JSON вручную, повторить bootstrap из чистого commit. |
| `Versioned rebuild script must be a plain file`; `Versioned Install-TaskTrackerV3.ps1 is not yet present` | Восстановить версионируемые скрипты в чистом клоне и повторить bootstrap. |
| `Existing service binding differs`; `Existing service.json is missing`; `Installed file must be plain` | Сверить путь EXE, конфигурацию и сервисную учётную запись; восстановить повреждённую установку до повторного запуска. |
| `Python version setting must be a plain file`; `Invalid Python version setting`; `Requirements must be a plain file` | Восстановить версионируемые `.venv/python_version.txt` и `requirements.txt` как обычные файлы; версия имеет вид `python3.11`. |
| `Python launcher ...`; `Python 3.11 is unavailable`; `could not be selected`; `Python 3.11 is required` | Установить требуемую версию Python и Windows launcher `py.exe` либо изменить версионируемую настройку; автоматического скачивания и fallback нет. |
| `RuntimeRoot must be an empty plain directory`; `could not create the protected virtual environment`; `Virtual environment Python is missing` | Проверить права на защищённый staging, поддержку `venv` у выбранного Python и свободное место; убрать повреждённый staging штатной очисткой. |
| `pip could not install dependencies`; `Python/PyYAML import failed`; `Staged Python worker import failed` | Обеспечить доступный `pip` источник пакетов и совместимый PyYAML, затем повторить установку; проверить исходную ошибку импорта. |
| `C# compiler is missing`; `Staged ServiceHost compilation failed`; `Stable service EXE copy hash mismatch` | Проверить компонент .NET Framework 4 и компилятор, исходник C# и целостность установленного EXE; повторить после исправления. |
| `Cannot read published ai-marketplace`; `Published marketplace lacks`; `does not match ... release`; `lacks ... tool` | Опубликовать ожидаемые версии плагинов в Git marketplace и проверить доступ к его `master`; локальной правки кэша недостаточно. |
| `Codex ai-marketplace is not the expected Git source`; `Codex could not add`; `marketplace upgrade/list failed`; `plugin list/add failed`; `MCP ... tools are unavailable` | Проверить CLI `codex`, Git-источник marketplace, сеть, установленные версии и лог ошибки; обновить плагины штатным процессом от целевого пользователя. |
| `Run marketplace update as TargetUser`; `TargetUser credentials were not provided`; `TargetUser Codex marketplace update failed` | Выполнить обновление в профиле целевого пользователя или предоставить его учётные данные в защищённом запросе установщика. |
| `duplicate TRACKER_MCP_TOKEN`; `TargetUser env.txt has no TRACKER_MCP_TOKEN`; `MCP token copies disagree or one is missing`; `MCP token is empty`; `MCP token verification failed` | Остановить переустановку и согласовать две копии токена: в `.protected\mcp-token` и пользовательском `env.txt`; не выводить токен в журнал. |
| `Cannot rotate an absent MCP token`; `Installed Python MCP service configuration is required for token rotation`; `MCP service must be Running before token rotation`; `MCP endpoint did not accept the token` | Сначала завершить первичную установку и запустить службу; при ротации проверить конфигурацию, порт и состояние MCP, затем повторить отдельную команду ротации. |
| `Service did not reach Running/Automatic`; `Installed MCP did not accept the configured token` | Проверить журнал службы, её учётную запись, порт и обе копии токена; установщик попытается вернуть прежнюю версию. |
| `Installation failed; previous service restored`; `MCP token update rolled back` | Исправить исходную причину после двоеточия и повторить; прежнее состояние восстановлено. |
| `RECOVERY_INCOMPLETE`; `SCM failed to delete`; `newly created service is still registered`; `Refusing to remove a path outside protected Tracker` | Не запускать повторную установку вслепую. Сохранить каталог `.rollback-*`, проверить пути, службу, токен, ACL и учётную запись вручную под администратором. |
| `PLUGIN_UPDATE_INCOMPLETE` | Служба уже работает; сохранить `.rollback-*`, исправить доступ к Codex/marketplace и повторить обновление плагинов без ручной правки кэша. |

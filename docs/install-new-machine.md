# Установка Task Tracker на новом компьютере

Эта инструкция описывает **новую установку**. Пути клона, личных файлов и Tracker выбираются на месте. Не указывайте `-MigrateTasks`.

Нужны Windows, PowerShell 7, Node.js и .NET Framework C# compiler (`csc.exe`). Node.js и PowerShell должны находиться в каталогах, которые целевой пользователь не может менять: установщик проверяет ACL исполняемых файлов и их родителей. Bootstrap выполняется под целевым пользователем без повышения; защита каталогов и установка службы — под администратором, которым может быть другая учётная запись.

## 1. Подготовить личную структуру

Рекомендуемая схема (имена и диск замените на свои):

```text
<диск>:\AI\                         владелец Administrators
  <пользователь>\                 владелец Administrators
    Work\                         рабочие права пользователя
    TaskTracker\                  после установки владелец сервисная учётная запись
```

`Work` и `TaskTracker` — соседи. Пользователю можно дать полные рабочие права **на `Work`**, включая вложенные файлы и папки. На `AI` и личной папке нельзя оставлять ему `Delete`, `DeleteSubdirectoriesAndFiles`, `ChangePermissions` или `TakeOwnership`; ограниченные права чтения и записи на личной папке допустимы. Нельзя располагать Tracker внутри пользовательского профиля или внутри `Work`: тогда пользователь контролирует его родителя.

Администратор создаёт `AI`, личную папку, `Work` и `TaskTracker`. Для обоих родителей Tracker проверьте план защиты, затем примените его **сверху вниз**:

```powershell
$repo = Read-Host 'Полный путь к клону task-tracker'
$personalRoot = Read-Host 'Полный путь личной папки AI'
$trackerRoot = Join-Path $personalRoot 'TaskTracker'
$workRoot = Join-Path $personalRoot 'Work'
New-Item -ItemType Directory -Path $workRoot, $trackerRoot -Force | Out-Null
pwsh -NoProfile -File (Join-Path $repo 'scripts\Protect-TrackerParent.ps1') -TrackerRoot $personalRoot
pwsh -NoProfile -File (Join-Path $repo 'scripts\Protect-TrackerParent.ps1') -TrackerRoot $personalRoot -Apply
pwsh -NoProfile -File (Join-Path $repo 'scripts\Protect-TrackerParent.ps1') -TrackerRoot $trackerRoot
pwsh -NoProfile -File (Join-Path $repo 'scripts\Protect-TrackerParent.ps1') -TrackerRoot $trackerRoot -Apply
```

Первый вызов защищает `AI`, второй — личную папку. До каждого `-Apply` проверьте вывод `READY` и точный путь родителя. Скрипт меняет владельца родителя на Administrators, удаляет опасные разрешения, сохраняет исходный SDDL в `<repo>\.runtime\tests\protect-tracker-parent\` и сверяет ACL непосредственных детей. Если каталог уже защищён, выводится `ALREADY PROTECTED`. Права на `Work` задавайте отдельно **только на `Work`**. Установщик дополнительно проверит всю цепочку родителей.

## 2. Bootstrap под целевым пользователем

Откройте обычный PowerShell под владельцем личного Tracker. Добавьте в `%USERPROFILE%\.env\env.txt` строку `TRACKER_FOLDER=<полный путь к TaskTracker>`; такая строка должна быть ровно одна, остальные настройки сохраняются. Затем из любого места выполните:

```powershell
$repo = Read-Host 'Полный путь к клону task-tracker'
pwsh -NoProfile -File (Join-Path $repo 'scripts\Bootstrap-TaskTracker.ps1')
```

Bootstrap выводит путь к пользовательскому `task-tracker.json`, корень Tracker и имя службы. Передайте эти значения администратору. При конфликте или превышении лимита имени сервисной учётной записи используйте `-ServiceAccountName <другое_имя>`. Bootstrap не перезаписывает существующий JSON с другим корнем: перенос действующего Tracker требует полной переустановки.

## 3. Установка службы под администратором

В PowerShell администратора используйте путь JSON, который вывел bootstrap:

```powershell
$repo = Read-Host 'Полный путь к клону task-tracker'
$configPath = Read-Host 'Полный путь к task-tracker.json из bootstrap'
pwsh -NoProfile -File (Join-Path $repo 'scripts\Install-TaskTracker.ps1') -ConfigPath $configPath -ValidateOnly
pwsh -NoProfile -File (Join-Path $repo 'scripts\Install-TaskTracker.ps1') -ConfigPath $configPath
```

`-ValidateOnly` должен сообщить `VALID` для выбранного Tracker. Если он отклонил ACL родителя или путь к Node.js/PowerShell, исправьте права или установите программы в защищённое место; не обходите проверку. При первом запуске установщик запросит пароль новой локальной сервисной учётной записи, создаст автоматическую службу и запишет код и `service.json` внутри `TaskTracker\.protected`. Если каталог `TaskTracker` был заранее создан администратором, установщик передаст владение службе.

## 4. Подключение клиента

Под обычным токеном **целевого пользователя** нужен плагин `task-folder-workflow` версии 0.5.2 или новее. Убедитесь, что в `%USERPROFILE%\.env\env.txt` есть ровно одна запись `TRACKER_FOLDER` с корнем установленного Tracker. Полностью закройте и перезапустите Codex Desktop, затем проверьте службу:

```powershell
$rid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value.Split('-')[-1]
Get-Service -Name "TaskTracker-$rid" | Select-Object Name, Status, StartType
```

Ожидаются `Running` и `Automatic`. В Codex отдельно проверьте список опубликованных MCP-инструментов: среди них должен быть `resolve_task_folder`. Вызов `get_tasks_folder` должен вернуть `<TRACKER_FOLDER>\tasks`. Если клиент не подключается, администратор проверяет службу и журнал в `<TRACKER_FOLDER>\.protected\logs`. Простая правка `env.txt` или перенос каталога после установки не меняют закреплённый путь службы.

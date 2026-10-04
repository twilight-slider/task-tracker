# Task Tracker

Персональная Windows-служба для регистрации проектов и создания защищённых папок задач. Код можно клонировать в любой каталог; рабочий Tracker задаётся пользователем через `TRACKER_FOLDER` и хранится отдельно от репозитория.

**Установка на другом компьютере:** [docs/install-new-machine.md](docs/install-new-machine.md).

Для установки используются `Bootstrap-TaskTracker.ps1` и `Install-TaskTracker.ps1`. Установщик по bootstrap JSON показывает план защиты ACL через `-PrepareAcl` и применяет его через `-PrepareAcl -ApplyAcl`, вызывая `Protect-TrackerParent.ps1` и `Protect-TrackerTasks.ps1`. Для существующего `tasks` он показывает план импорта через `-ImportExisting` и применяет его через `-ImportExisting -ApplyImport`, вызывая `Migrate-TrackerTasks.ps1`. `-MigrateTasks` нужен только при переносе задач из прежней службы.

Служба, конфигурация, код и данные располагаются внутри выбранного Tracker. Плагин `task-folder-workflow` версии 0.5.3 или новее предоставляет операции с папками задач, а `task-tracker-mcp` версии 0.1.0 или новее — операции со снимками. Оба читают единственный `TRACKER_FOLDER` из `%USERPROFILE%\.env\env.txt` и запускают `<TRACKER_FOLDER>\mcp-adapter.js` с разными областями инструментов. После изменения этой записи полностью перезапустите Codex Desktop и проверьте наличие `resolve_task_folder` и `create_result_snapshot`.

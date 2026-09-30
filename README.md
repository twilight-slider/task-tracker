# Task Tracker

Персональная Windows-служба для регистрации проектов и создания защищённых папок задач. Код можно клонировать в любой каталог; рабочий Tracker задаётся пользователем через `TRACKER_FOLDER` и хранится отдельно от репозитория.

**Установка на другом компьютере:** [docs/install-new-machine.md](docs/install-new-machine.md). Начинайте с неё: команды в исторических отчётах AIDEV-58/AIDEV-60 содержат пути прежнего компьютера.

В новой установке используются `Bootstrap-TaskTracker.ps1`, `Protect-TrackerParent.ps1` и `Install-TaskTracker.ps1`. Скрипты с именем `TaskFolderMcp` и `Register-TaskFolderProject.ps1` относятся к прежней установке; для нового Tracker их не запускают. `-MigrateTasks` нужен только при переносе задач из прежней службы.

Служба, конфигурация, код и данные располагаются внутри выбранного Tracker. Плагин `task-folder-workflow` версии 0.5.0 подключается к `<Tracker>\mcp-adapter.js` через пользовательскую переменную `TASK_FOLDER_MCP_ADAPTER`.

Рабочая миграция Vasil и её проверки описаны отдельно в [docs/aidev60-installation.md](docs/aidev60-installation.md). Архитектура прежней службы сохранена в [docs/architecture.md](docs/architecture.md) как исторический материал.

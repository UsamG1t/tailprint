# TailPrint

TailPrint публикует принтеры в сети Tailscale. Агент на Linux, macOS или Windows обнаруживает системные очереди и принимает задания. Устройства с iOS и Android отправляют документы из браузера того же tailnet.

Поддерживаются PDF, DOC, DOCX, XLS, XLSX, JPEG, PNG и автономные векторные SVG. Доступны масштаб, ориентация, диапазоны страниц, несколько страниц на листе, двусторонняя печать и брошюры.

[Руководство](docs/index.md) · [GitHub Pages](https://usamg1t.github.io/tailprint/) · [Готовые пакеты](https://github.com/UsamG1t/tailprint/releases/latest)

## Установка

Требуется Python 3.10 или новее. Выполните в активированном виртуальном окружении:

```sh
python -m pip install .
tailprint
```

Для установки из релиза скачайте файл `.whl` и передайте его путь команде `python -m pip install`. На Linux и macOS агент использует CUPS, на Windows — SumatraPDF и системный драйвер. Для документов Word и Excel нужен LibreOffice на узле принтера.

1. Подключите устройства к Tailscale и запустите агент на компьютере с принтером.
2. Откройте `http://127.0.0.1:8787`, перейдите в **Принтеры узла** и нажмите **Опубликовать** у нужной очереди.
3. На другом устройстве откройте адрес Tailscale, показанный агентом, выберите принтер и отправьте документ.

Для просмотра интерфейса без оборудования запустите `tailprint --demo`.

## Разработка

```sh
python -m pip install -e .
python -m unittest discover -s tests -v
python -m pip install build
python -m build
```

GitHub Actions проверяет пакет на Linux, macOS и Windows. Для релиза задайте версию в `pyproject.toml` и отправьте соответствующий тег `vX.Y.Z`. После успешных проверок GitHub Releases получит wheel и исходный дистрибутив. Руководство собирается из `docs/` при изменении документации в `master`.

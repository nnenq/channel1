# Помощник по перезаливу своих Shorts

Берёт **самые просматриваемые** шортсы с указанных каналов, делает лёгкую
уникализацию и публикует их на твоём целевом канале через официальный YouTube Data API.

Уникализация (всё настраивается в `config.yaml`):

| Эффект | По умолчанию |
|---|---|
| Увеличение изображения | `zoom: 1.05` (+5%) |
| Поворот | `rotate_deg: -0.5` (чёрные углы срезаются автоматически) |
| Тени | `shadows: 0.10` — тени осветляются на 10% |
| Размытие сверху и снизу | `edge_blur: {height: 0.10, sigma: 18}` — по 10% высоты кадра |
| Метаданные исходника | удаляются |

Уже перезалитые видео запоминаются в `state/<задача>.json`, поэтому каждый
следующий запуск берёт следующее по просмотрам видео, а не то же самое.

## Установка

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
```

ffmpeg ставить не нужно: если его нет в системе, берётся из пакета `imageio-ffmpeg`.

## Доступ к YouTube (один раз)

1. Открой [Google Cloud Console](https://console.cloud.google.com/), создай проект.
2. **APIs & Services → Library** → включи **YouTube Data API v3**.
3. **OAuth consent screen**: тип External, добавь свой Gmail в **Test users**.
4. **Credentials → Create credentials → OAuth client ID** → тип **Desktop app**.
   Скачай JSON и положи в корень проекта как `client_secret.json`.
5. Привяжи целевой канал (куда заливать):
   ```bash
   python -m reuploader auth --token tokens/happyflick.json
   ```
   В браузере выбери именно **новый канал** (если у аккаунта несколько каналов/брендов).

`client_secret.json`, `tokens/`, `state/`, `work/` и `config.yaml` уже в `.gitignore`.

## Использование

```bash
# посмотреть топ шортсов канала по просмотрам
python -m reuploader top https://www.youtube.com/@HappyFlick-b4e/shorts

# проверить: скачать + обработать, но НЕ заливать (результат в work/)
python -m reuploader run --dry-run

# залить (per_run видео для каждой задачи из config.yaml)
python -m reuploader run

# только одна задача / взять видео с другого канала разово
python -m reuploader run --job happyflick
python -m reuploader run --job happyflick --source https://www.youtube.com/@OtherChannel

# применить эффекты к своему локальному файлу, чтобы подобрать настройки
python -m reuploader process input.mp4 output.mp4
```

## Масштабирование

- **Больше каналов** — добавь задачи в `jobs:` в `config.yaml`: у каждой свой
  `token` (целевой канал), свои `sources`, `per_run`, `privacy` и `effects`.
  Для каждого нового целевого канала один раз выполни `auth` с новым путём токена.
- **Автоматически по расписанию** — cron (Linux/macOS):
  ```cron
  0 */6 * * * cd /path/to/channel1 && .venv/bin/python -m reuploader run >> reuploader.log 2>&1
  ```
  На Windows — «Планировщик заданий» с той же командой.
- **Квота API**: загрузка одного видео стоит 1600 единиц, по умолчанию дают
  10 000 в день на проект → **~6 заливок в сутки на один Google Cloud проект**.
  Для большего объёма — несколько проектов (свой `client_secret` в задаче)
  или запрос на увеличение квоты.
- Пока приложение в режиме «Testing», refresh-токен живёт ~7 дней — потом
  повтори `auth`. Чтобы это убрать, опубликуй приложение на экране OAuth consent.
- Загрузки из непроверенного (unverified) API-проекта YouTube может ставить
  в «private», пока проект не пройдёт аудит YouTube API.

## Важно

Инструмент рассчитан на **твой собственный контент**. Учитывай, что YouTube
может посчитать одинаковые видео на разных каналах «повторно используемым
контентом» (это влияет на монетизацию), даже с уникализацией.

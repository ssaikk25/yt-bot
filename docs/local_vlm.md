# Локальный VLM pre-filter (экономия вызовов MiMo)

Локальная vision-модель первым смотрит на **те же кадры**, что уже извлечены
`app/frame_sampler.py`, и выносит консервативный вердикт об **очевидных** визуальных
дефектах. Цель — снять часть платных вызовов MiMo, а не заменить MiMo целиком.

> По умолчанию `LOCAL_VLM_ENABLED=false`: **старое поведение проекта не меняется**.

---

## 1. Что делает модуль

- `app/local_vlm.py` — клиент (`LocalVlmClient.analyze_frames`) + трекер сравнения
  (`LocalVlmTracker`). Работает через OpenAI-совместимый сервер llama.cpp
  (`/v1/chat/completions`), **без** внешних AI API и **без** лишних обращений к YouTube.
- `app/local_vlm_benchmark.py` — benchmark на уже извлечённых кадрах.
- Интеграция — в `app/channel_evaluator.py`: локальная модель вызывается после
  извлечения кадров, перед MiMo QUICK.

### Формат ответа модели (строгий JSON)

```json
{
  "verdict": "OBVIOUS_REJECT | UNCERTAIN | NO_OBVIOUS_REJECT",
  "flags": ["watermark","borders","overlay_text","blur","pixelation","too_dark","collage","slideshow","other"],
  "evidence": [{"frame": "10|50|90", "observation": "что конкретно видно"}],
  "reason": "краткое обоснование"
}
```

- `OBVIOUS_REJECT` — дефект явно виден и доминирует в кадре.
- `NO_OBVIOUS_REJECT` — очевидного дефекта нет (это **не** значит, что видео подходит).
- `UNCERTAIN` — неуверенность или незначительный/спорный дефект.

Модель обязана ссылаться на конкретный кадр; решение по заголовку запрещено.
`confidence` от самой модели не используется как вероятность. Временные признаки
(тряска/скорость/статичность) и AI-generated по отдельным кадрам модель не оценивает.

---

## 2. Режимы

| `LOCAL_VLM_MODE` | Поведение |
|---|---|
| `shadow` (default) | Локальный анализ выполняется и **сравнивается** с MiMo; PASS/FAIL видео и канала не меняется. |
| `active` | Авто-reject заменяет вызов MiMo **только** по `LOCAL_VLM_AUTO_REJECT_FLAGS` (пусто = выключен). |
| `off` | Локальный анализ не выполняется. |

В любом режиме локальная модель **никогда** не ставит каналу PASS — она может только
пометить отдельное видео как явный reject (active), либо отдать решение MiMo (shadow/uncertain/error).

---

## 3. Что скачать (SmolVLM2-2.2B-Instruct Q4_K_M)

Целевое железо: 16 ГБ RAM, RTX 3050 Laptop 4 ГБ VRAM.

### 3.1. llama.cpp (сервер)

Скачайте **prebuilt Windows CUDA** бинарник с релизов llama.cpp:

- https://github.com/ggml-org/llama.cpp/releases
- Файл вида `llama-<version>-bin-win-cuda-cu12.2-x64.zip` (или `cu13`).
- Распакуйте, нужен `llama-server.exe` (+ DLL рядом).

> Альтернатива (CPU-only): `llama-<version>-bin-win-x64.zip` — медленнее, но не зависит
> от CUDA и точно влезает в 16 ГБ RAM.

### 3.2. Веса модели (два файла)

На странице модели HuggingFace вкладка **Files**:

- https://huggingface.co/HuggingFaceTB/SmolVLM2-2.2B-Instruct

Скачайте **два** файла одной версии (не смешивайте репозитории):

1. квантованный vision-LLM: `...-q4_k_m.gguf` (например `smolvlm-2.2b-instruct-q4_k_m.gguf`);
2. мультимодальный проектор: `...-mmproj-f16.gguf` (например `smolvlm-2.2b-instruct-mmproj-f16.gguf`).

Точные имена подтвердите на странице (в списке `.gguf`). Итого примерно 2–3 ГБ.
Для сравнительного теста допускается Qwen2.5-VL 3B, но **не скачивайте сразу несколько
больших моделей** — сначала доведите одну.

### 3.3. Запуск сервера

```bash
llama-server.exe -m smolvlm-2.2b-instruct-q4_k_m.gguf \
  --mmproj smolvlm-2.2b-instruct-mmproj-f16.gguf \
  --host 127.0.0.1 --port 8080 --ctx-size 4096 \
  --n-gpu-layers 999 --parallel 1
```

- `--n-gpu-layers 999` — все слои на GPU. Если не хватает 4 ГБ VRAM (OOM):
  попробуйте `--n-gpu-layers -1` (авто) или уменьшите число; крайний случай —
  `--n-gpu-layers 0` (CPU-only, 16 ГБ RAM хватит, но медленнее).
- `--parallel 1` — один слот inference (соответствует `LOCAL_VLM_MAX_CONCURRENCY=1`).
- Проверка: `curl http://127.0.0.1:8080/health` → `{"status":"ok"}`.

### 3.4. Конфиг `.env`

```ini
LOCAL_VLM_ENABLED=true
LOCAL_VLM_MODE=shadow
LOCAL_VLM_BASE_URL=http://127.0.0.1:8080/v1
LOCAL_VLM_MODEL=smolvlm2-2.2b
LOCAL_VLM_API_KEY=
LOCAL_VLM_TIMEOUT_SECONDS=90
LOCAL_VLM_MAX_CONCURRENCY=1
LOCAL_VLM_MAX_IMAGE_SIDE=512
LOCAL_VLM_MAX_OUTPUT_TOKENS=200
LOCAL_VLM_AUTO_REJECT_FLAGS=
```

---

## 4. Benchmark

Использует кадры, **уже лежащие в `temp/`** (никаких обращений к YouTube):

```bash
# только локальная модель (без траты токенов MiMo) — быстрый sanity-check
python -m app.local_vlm_benchmark --only-local --limit 20

# shadow-сравнение: локальная модель И MiMo на одних и тех же кадрах
python -m app.local_vlm_benchmark --limit 20

# отчёт по уже накопленному JSONL (без запуска моделей)
python -m app.local_vlm_benchmark --report
```

Скрипт печатает VRAM (`nvidia-smi`) и RAM (через `psutil`, если установлен:
`pip install psutil`) до/после прогона, среднее время локального анализа,
вердикты, false-reject rate и precision по каждому признаку.

**Критерий успеха** — не «много отказов», а сколько MiMo-вызовов можно безопасно убрать
(низкий false-reject при достаточном числе подтверждённых reject).

---

## 5. Статистика shadow-режима

- `logs/local_vlm_compare.jsonl` — по строке на видео: локальный вердикт + итог MiMo.
- `logs/local_vlm_summary.json` — агрегаты: вердикты, задержка, ошибки, false-reject,
  precision по каждому признаку.

### Какие признаки можно авто-reject (только после валидации)

Кандидаты на высокую precision (проверяются на shadow-выборке): `watermark`,
`borders`, `overlay_text`, `collage`, `slideshow`, `too_dark`.

**Не** включайте в авто-reject без отдельной валидации: `blur`, `pixelation` (могут быть
художественными/сжатием), а также `other` и всё, чего нет в enum (ai_generated,
talking_head, скорость монтажа, динамика). Начинайте с одного-двух признаков с
precision ≈ 1.0 и сотнями примеров.

```ini
# пример: только после того, как shadow-отчёт показал precision ~1.0
LOCAL_VLM_MODE=active
LOCAL_VLM_AUTO_REJECT_FLAGS=watermark,collage
```

---

## 6. Совместимость с правилом 30%

- **shadow**: локальный ответ не влияет на denominator, на `checked/suitable` и на
  раннюю остановку — видео идёт в MiMo как обычно.
- **active**: локально подтверждённый reject (по allowlist) учитывается как
  «не подходящее» видео (`checked += 1`, `suitable=False`) — как обычный FAIL по MiMo.
- Локальный `UNCERTAIN`, ошибка inference или недопустимый JSON → видео идёт в MiMo
  (не считается FAIL, не считается temp). Технически непроверенное видео (probe/ffmpeg)
  по-прежнему не считается контентным FAIL. Denominator = `eligible` минус unavailable.
- Локальная модель не влияет на выбор категории и на финальный PASS канала.

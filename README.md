# 涨潮断面通行簿

内河调度员用潮尺、站点基准和船期表核对放行时间。工作台默认打开断测场次，左侧可依次查看采信、测站归一、阈值交点、全程潮窗、船期覆盖、运行留档和证据签发。

## 启动

```sh
docker compose up --build
```

访问 `http://localhost:18472`。本机也可安装 `requirements.txt` 后执行 `python -m app.main`。留档存入 `TIDE_DB` 指定的 SQLite 库，默认 `var/tide.sqlite3`。清空演示归档时先停止应用，再移除该库或 Compose 命名卷。

## 复核

1. 首页选海棠17，核对 3000 秒窗口中的 1500 秒可行、900 秒不可行、600 秒缺证。
2. 打开全程可行潮窗，查看放行起止、临界点和断面潮尺行。
3. 打开运行留档，复算并保留原归档号，再到潮窗证据签发下载 JSON 或打印。
4. 切换连续测站样例，海棠17的全部 3000 秒均可行。

## 值班裁决

断测场次可打开 `/desk/review`（或 `/review`）。每条 `SENSOR_BAD`、冲突等异常给出“只剔坏行”和“停用整站”两种处置，并按船期列出相对另一方案变化的可行、缺证区间与秒数。

- `POST /api/reviews` 用 `{"sample":"dirty"}` 或 `{"sources":{...}}` 开复核簿；也可直接传 `run_id`。
- `GET /api/reviews/<review_id>` 查异常、两种影响、当前处置和签发限制。
- `POST /api/reviews/<review_id>/decisions` 提交 `{"anomaly_id":"...","action":"row_only|disable_station"}`；支持 `Idempotency-Key` 和 `version`。
- `POST /api/reviews/<review_id>/readings` 追加晚到读数（单条字段、`readings` 列表或 `csv`）。若异常夹逼范围改变，自动重算并把旧处置置为 `void`/`superseded`；`GET .../history` 与 `GET /api/decisions/<id>` 仍可查询。
- 同一异常的同处置重试返回 `reused:true`，不新增记录；并发不同处置由 SQLite 唯一活动处置索引仲裁，仅一份返回 201，另一份返回 409 `DECISION_CONFLICT`。
- 生效中的 `disable_station` 若造成可行窗变为缺证/不可行，`GET/POST /api/export/<run_id>`、`/api/sign/<run_id>` 会以 409 `STATION_DISABLED` 拒绝签发；复核页和验签页同步禁用签发入口。

`GET /api/stage/{readings,datum,crossings,windows,coverage}` 可查看样例阶段结果。对同路径执行 POST 可提交 `{"sources":{"observations":"...","stations":"...","vessels":"..."}}`。`POST /api/journal` 接受同一输入或 `{"sample":"dirty"}`。`GET /api/journal/<id>` 查归档，`POST /api/journal/<id>/rerun` 复算，`GET /api/export/<id>` 下载证据。

字段见 [数据字典](docs/DATA-DICTIONARY.md)，规则与回归见 [深度登记](docs/DEPTH.md)。本机运行 `python -m pytest -q`。

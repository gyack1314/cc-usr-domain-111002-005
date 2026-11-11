# 级联筛选控件下的条件组合采集本地门户

本项目是可在本地运行的合成业务门户，提供真实浏览器页面、交互状态及样本资料，不包含业务采集端。资料、用户与工作空间都是虚构内容。

## 环境

Python 3.12、Playwright 1.56.0 与 Chromium。`portal/` 是标准库 HTTP 服务，`web/` 是浏览器页面及 JavaScript 交互，`tests/` 检查门户自身行为。Python 用于服务与浏览器控制，JavaScript 只在浏览器内运行，不需要 Node.js 服务。

安装依赖及浏览器：

```sh
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python -m portal --port 8090
```

在浏览器打开本机8090端口即可操作。门户的正常交互在页面完成，`web/data.json` 保存合成来源材料。状态随页面或浏览器上下文生命周期保存；需要持久状态的功能使用当前 origin 的 localStorage。重新开始可创建新的浏览器上下文。

运维入口 GET `/__state` 查看来源动作记录，POST `/__control` 用 JSON 设置 phase、version、confirmed。它们只用于本地来源调度，接入程序的正常操作不能借用运维入口取得业务数据或完成授权。GET `/api/runtime` 和 POST `/api/action` 是门户页面内部协议。

## 验证与容器

```sh
python -m unittest discover -s tests -v
python -m compileall -q portal tests
docker build -t cascading-filters-portal .
docker run --rm --network none --shm-size=1g cascading-filters-portal
```

Dockerfile 固定浏览器基础镜像的完整摘要，依赖版本在 requirements.txt 固定。首次构建需要下载依赖；镜像完成后，上述检查禁用外网，浏览器与门户在同一容器内通信。默认运行来源检查，不运行资料采集。

如需在宿主浏览器操作容器内门户：

```sh
docker run --rm -p 127.0.0.1:8090:8090 cascading-filters-portal python -m portal --host 0.0.0.0 --port 8090
```

页面的 DOM 状态、公开身份属性与中文加载提示可用于等待完成，避免使用固定秒数等待。来源动作记录可核对页面操作；不要在日志记录会话秘密或不需要的私密字段。

来源场景、属性及控制方式详见 `source-contract.json`。

## 资料采集模块

`collector/` 是按筛选意图遍历级联组合的浏览器采集端，只用 Playwright 读取页面 DOM 与结果，不借用 `/api/*`、`/__*` 后台接口枚举选项或读取结果。

意图配置（见 `intent.example.json`）：`regions`/`kinds`/`projects` 支持 `"all"`（当前可见全部）、标签字符串（同名标签展开为多个，按各自 value 定位）或 `{"value": ...}`/`{"label": ...}`；`max_attempts` 与 `timeout_ms` 控制有界重试。不在可见范围的输入会写入进度并报告为“不可满足”，不会退回“全部”。

```sh
python -m collector --intent intent.example.json --out outputs/run
python -m collector.demo            # 演示: 同名区域、乱序门闩、失败/空分支
```

采集行为：选择一律按 option 的 value 定位并核对所属父键；父项变化后等待 `data-loading` 落定并核对控件与选项父键，旧响应晚到造成的污染会触发从有效父项重建依赖的有界重试；查询后等待 `#signature` 出现新的 request_id，并确认 `#results` 绑定同一 request_id 且签名与已确认选择一致才采集。空选项分支记录为“空分支”并留下查询证据（签名与 request_id），加载失败分支记为“失败”，两者在进度表中区分。

导出（`--out` 目录）：`report.json`（意图、组合进度、命中关系、资料）、`progress.md`（组合进度表）、`materials.json`（资料正文按编号@内容版本）、`hits.json`（各签名命中的资料编号）。`python -m collector.demo` 在 `outputs/demo/` 下产出三个场景的导出。

回归检查 `tests/test_collector.py` 覆盖：全部有效组合与页面可见结果签名的对应核对、同名区域按 value 区分、乱序门闩下旧选项不污染新查询、失败分支有界重试与恢复、选项被移除后的重建、不可满足输入报告、采集端不访问运维入口。

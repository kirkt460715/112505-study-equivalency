# 跨村研学的替代课核验与结业发证

学校与合作教育点使用演练人员编号。班级对应桥接表示待核身份。实际出席、教研复核和学校签发是不同来源的记录。

`fixtures/registers.json` 提供演练台账，`fixtures/events.ndjson` 提供各端送达的独立消息，`fixtures/receipts.json` 提供合作方的模拟回执。事件游标为送达顺序。业务发生时点出现在消息内容中时，以其来源所注明的时点为准。整套资料均为合成数据。

## 本地联调

使用 Python 3.11.2 标准库。联调入口只监听本地地址，不需要账号或外部连接。

```sh
python3 -m unittest -v
python3 fixture_gateway.py --port 8765
```

`GET /v1/registers` 返回台账。用 `GET /v1/events?after=0&limit=50` 读取一页消息，后续页将上一页的 `next_cursor` 作为 `after`。每条消息有 `cursor`、`source`、`minute` 及原业务记录 `payload`。

对端发送协议为 `POST /v1/receipts`，正文提供 `reference`、`operation`、`recipient`，三个字段均为字符串。需要配对的操作与接收方见回执示例文件。返回对象含 `reference` 和对端 `receipt`。若条目设置 `lost_reply`，首次调用被记录后连接断开。可用 `GET /v1/receipts/<reference>` 查看已记录的回执。相同编号和内容可重试，编号被另一个操作使用时返回 409。

联调进程运行期间保存回执记录。需要试读其他台账、消息与回执时，可通过 `--fixtures` 指向另一组同名资料文件。合作方只返回模拟事实，业务办理功能由各方应用负责。

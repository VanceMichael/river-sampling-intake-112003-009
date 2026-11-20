# 河道断面监测和冷箱交接

资料服务模拟多个遥测柜、值班水文单和巡河车调度台。所有时间为同一虚构监测日的北京时间。运行依赖 Python 3.11 标准库。

```sh
python3 source_service.py --port 8040
python3 -m unittest discover -s tests -v
```

从 `GET /` 读当前时间。演练人员用 `POST /clock` 发送形如 09:05:00 的 time 向前拨。收件端重启不会清空台账，装置服务重新启动后重来一场演练。

## 监测与机箱

`GET /station?station=Q4` 给已见触发的摘要，local_time 为机内时刻。一次触发身份为柜号、开机世代及 id。遇到修订重复出现相同触发身份，不能当成再次发生。摘要只用于追取和识别触发，不能证明已收实物样。

`GET /radio?cab=M1` 给已见页的标识、可用通道和页首尾序号。gaps 中 wait_relay 表示有待另一无线传送，no_records 表示柜端确认该段没有页，offline 表示目前柜端尚未补报。发布修复文件时 replaces 明确替换旧页版本。页内序号在同一次开机里递增，首尾含义是闭区间。缺段不得把其前后机内位置视为连续证据。`GET /radio/slots` 给当前及未来的可用传输班，通道班时刻区间左闭右开。按 `GET /download?slot=T1&page=A-In` 领页，同一通道班有实际可用页数，响应摘要为完整 HTTP 正文字节的 SHA-256。425 表示在封写，410 已被淘汰，渠道不可连为 503，空缺可继续等清单或另一通道。素材文件由演练服务按实际档位公开。

日内 `/clocks` 以 cab、epoch 和 local 窗口定位校对，将机内时分秒分别加上 offset_seconds 的两端得到可能真实时刻。local 左闭右开，时钟的可能区间是闭的。事件 kind=position 记真实瓶位，valve 的 jam 记卡位，restart 只界定新世代。pulse 的 step 引用同次开机前一证据的序号。position 要能对应到触发时的位置，卡位和没有连续证据都不能证明进入实物瓶，offset 是设备原样步进计数。bottle_set 是车上冷箱的空间号，不是瓶位。

## 到访、配对和票据

`/weather` 公开事件、断面、版次及左闭右开的阶段窗。`/duty` 公开单窗需要和 PAIR 的上下游阶段，上下游可能发生时刻的差须保守落在 travel_seconds 的闭区间方可认为已证成配对。实时仍需补采的窗口由工单 arrive_by 指定最晚实际到场时刻，配对成员也按其相应窗口约束。最晚到场时刻含当刻。

`/boxes` 给同柜世代的瓶位映射和实际冷箱资料。空白样可覆盖的具体瓶由 covers 限定，箱、断面、柜和世代共同标识实物；新资料只替换同一箱上公告的字段。接受且在覆盖范围才有效，expected_delivery 与 cold_by 用于核对送达可行性。计划送达可以证明当前可以送检，只有 `/lab` 的收件回条才代表真的送到了化验室。迟到或退回会改变当前可用性，原送检回条可按 basis 查证。field_sample 以调度台确认的时间和真实瓶箱为准。

`GET /trips` 给各车可去断面、实际到达时刻、预约截止和车上箱位。含截止时刻可约。`POST /trip/book` 接收 request、trip、station、bottle_set、event、phase。一个箱位在同一车次只能用于一份已接受任务。重复提交相同请求号只允许同内容。503 表示可能已经占用，需要从 `GET /trip/receipt?request=...` 继续核对。已经出发的补采保留其中 receipt 和 travel，后续实际现场记录另以 actual 登记。

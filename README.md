# 暴雨水情自动采样资料

本地演练资料使用虚构的实物、机构和记录编号。经营单据存放于 `data/`，包括 `weather.json`、`clocks.json`、`boxes.json`、`duty.json`、`dispatch.json`。来源原件在提供时由资料端送出。

## 资料约定

窗口用北京时并左闭右开，仍可能落在两个窗口的触发不能作为任一窗口的确定采样。offset_seconds=[min,max] 将机箱时刻映射为现场时刻范围。分瓶转动由底盘步进与瓶签两端记录界定；confirmed 表示清点，jam 使当次注水未知，empty 表示确认未装到样。空白样用于清单列出的箱与瓶，不跨断面。补采窗口截止以值班日程的 arrive_by 为准。

根路径列出资料入口。各路径每次请求依次返回该路径的演练响应，最后一条响应保持不变。不同路径有各自的请求计数。重启资料端从首条响应开始。路径清单中的链接相对同一服务。非演练路由的原件对应 `originals/` 下同名路径，经营单据也可以从 `/data/文件名.json` 直接领取。

## 运行资料端

运行依赖为 Python 3.11 标准库。

```sh
python3 source_service.py --port 8040
```

以下命令核对本地资料服务和交互。

```sh
python3 -m unittest discover -s tests -v
```

**目前还在开发测试中，请勿使用。**

插件清单支持 dependencies、optionalDependencies、conflicts 和 exclusiveResources，数据库校验声明并写入 catalog.json。声明关系时使用 SemVer；跨源依赖用 `{ "version": "^1.0.0", "source": "https://example.org/catalog.json" }`，source 是目录地址而非订阅。Pages 仍仅发布目录清单。


插件仅使用 `tags`，不再提供分类。生成目录时自动生成 `tags.json`：`{"schemaVersion":1,"tags":["工具","温控"]}`。标签聚合当前插件、按字符串精确去重并排序，插件删除或移除标签后下次生成自动清除，无须手动维护。stable/testing 分别发布 `catalog.json` 与 `tags.json`，程序获取多个插件源后自行聚合去重，不发布跨渠道并集文件。旧清单中的 `category` 仅兼容读取，不进入新目录。Pages 仍不保存包或图片。

## 可选安装版本

发布时从 publish 分支的上次目录继承已校验的历史版本，每个插件最多保留 20 个版本。`plugins` 数组允许相同 ID 的不同版本；当前登记版本排第一，作为推荐版本，后续项保留各自下载地址、SHA256、运行用户和依赖声明。Framely 商店把它们合并到一个插件卡片，在详情中选择安装版本。重复 ID 与版本组合、历史版本改包均拒绝。移除插件登记后，其历史版本也退出目录。Pages 仍只保存 JSON，旧包继续从作者的下载地址获取；旧附件失效时安装明确报错。

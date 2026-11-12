# 添加新作品

一部作品对应 `images/` 下的一个文件夹，例如 `images/原神/`、`images/星穹铁道/`、`images/崩坏3/`。每个文件夹都是自包含的：清单、机器清单和图片都在里面，下载时只打包这一个文件夹。

## 步骤

1. 在 `images/` 下新建文件夹，名称使用作品的常用中文名，不要再加一层分类。
2. 复制 [examples/作品模板/00-清单.md](../examples/作品模板/00-清单.md) 到新目录，保持文件名为 `00-清单.md`（这样在 GitHub 打开该文件夹时清单会排在最前）。
3. 按角色逐行填写名单。有图片时写成 `[角色名](角色名.jpg)`；其他版本写成 `[角色名（演出服）](角色名（演出服）.webp)`。勾选表示已收集。
4. 在仓库根目录运行：

   ```sh
   python3 tools/validate_catalog.py images/作品名
   ```

   这会在该文件夹生成或更新 `catalog.json`。来源网址等人工信息可写在机器清单里，之后重新生成会保留已有 `id` 和来源。
5. 提交时同时包含清单、机器清单和图片。用 `python3 tools/validate_catalog.py --check --all` 确认没有漏提交。

## 下载

清单全部勾选后，再单独下载该文件夹，例如用 [download-directory](https://download-directory.github.io/) 指向该目录，或在已克隆的仓库里执行 `zip -r 作品名.zip images/作品名`。不要把整个仓库打成一个包。

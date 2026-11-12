# 项目协作

- 先读 `README.md`、`docs/design.md`、`docs/catalog-contract.md`、`docs/image-guidelines.md`、`docs/add-work.md` 和 `NEXT.md`，接续已有进度。
- 这是图片中转站，不是已经实现的收集器或酒馆扩展。每个作品使用 `images/<作品>/` 下的独立文件夹，不要再写根目录总清单。
- `00-清单.md` 是任务名单和目录，也是机器清单的唯一来源。保持这个文件名，以便在 GitHub 打开文件夹时排在最前。角色名写成指向图片的相对链接；勾选表示已收集，未勾选表示尚未收集或不合格。
- 名单使用角色名，作品名来自文件夹；生成的 `roster_name` 为 `角色名（作品）`。`原神·温迪` 与 `温迪（原神）` 等写法留给未来酒馆查找，不要为此改名单。
- 图片以角色是否能辨认为准，大小 500 KB 至 2 MB。勾选前先运行校验脚本。来源网址可保留在 `catalog.json`，重新生成时不要丢掉已有 `id` 和来源。
- 新增作品时复制 `examples/作品模板/`，按 `docs/add-work.md` 操作。下载时只打包该作品文件夹，不要把整个仓库打成一个包。
- 在仓库根目录运行 `python3 -m unittest discover -s tests -v` 和 `python3 tools/validate_catalog.py --check --all`。需要更新机器清单时去掉 `--check`。
- 不要提交凭据或私人聊天内容。

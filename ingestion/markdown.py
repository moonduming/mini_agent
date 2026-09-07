import re
import yaml
from pathlib import Path
from transformers import AutoTokenizer


TOKENIZER = AutoTokenizer.from_pretrained(
    "BAAI/bge-m3",
    local_files_only=True,
)

def count_tokens(text: str):
    return len(TOKENIZER.encode(text, add_special_tokens=False))


def preprocess_markdown(text):
    metadata = {}

    # 1. 提取 YAML Front Matter
    # 只认为文件开头的 --- ... --- 是 Front Matter
    front_matter_pattern = r"\A---\s*\n(.*?)\n---\s*(?:\n|$)"
    match = re.match(front_matter_pattern, text, flags=re.DOTALL)

    if match:
        front_matter = match.group(1)
        # YAML -> Python dict
        metadata = yaml.safe_load(front_matter) or {}
        # 从正文中删除 Front Matter
        text = text[match.end():]

    # 2. 删除 HTML 注释
    # 可以匹配跨行<!-- ... -->
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # 3. 清理因为删除内容产生的大量连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)

    return metadata, text


def read_file(file_path: str):
    with open(file_path, "r", encoding='utf-8') as f:
        return f.read()


def tail_overlap_block(text: str, overlap_tokens: int) -> dict | None:
    """截取上一 chunk 的 token 尾部，保证下一 chunk 一定有前文承接。"""
    if overlap_tokens <= 0:
        return None

    token_ids = TOKENIZER.encode(text, add_special_tokens=False)
    overlap_text = TOKENIZER.decode(
        token_ids[-overlap_tokens:],
        skip_special_tokens=True,
    ).strip()
    if not overlap_text:
        return None
    return {"type": "overlap", "content": overlap_text}


def merge_blocks(
    blocks: list,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
):
    chunks = []
    current_blocks = []

    for block in blocks:
        candidate_blocks = current_blocks + [block]
        candidate_text = "\n\n".join(
            item["content"]
            for item in candidate_blocks
        )
        if count_tokens(candidate_text) <= max_tokens:
            current_blocks.append(block)
            continue

        # 当前 block 加进去会超限
        if current_blocks:
            chunks.append({
                "content": "\n\n".join(
                    item["content"]
                    for item in current_blocks
                ),
                "types": [
                    item["type"]
                    for item in current_blocks
                ],
            })

        # 保留上一 chunk 的 token 尾部，避免较大的表格或代码 block
        # 导致完整 block overlap 放不下、下一 chunk 完全没有前文。
        previous_text = "\n\n".join(item["content"] for item in current_blocks)
        overlap = tail_overlap_block(previous_text, overlap_tokens)
        current_blocks = ([overlap] if overlap else []) + [block]

        # 单个 block 本身已经接近上限时，不强行加入 overlap。
        current_text = "\n\n".join(item["content"] for item in current_blocks)
        if count_tokens(current_text) > max_tokens and overlap is not None:
            current_blocks = [block]

    # 最后一组
    if current_blocks:
        chunks.append({
            "content": "\n\n".join(
                item["content"]
                for item in current_blocks
            ),
            "types": [
                item["type"]
                for item in current_blocks
            ],
        })

    return chunks


def split_markdown_blocks(
    content: str,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
):
    lines = content.splitlines()
    blocks = []
    i = 0

    # - item
    # * item
    # + item
    # 1. item
    # 1) item
    list_pattern = re.compile(
        r"^\s*(?:[-+*]|\d+[.)])\s+"
    )
    # | --- | :---: | ---: |
    table_separator_pattern = re.compile(
        r"^\s*\|?\s*:?-{3,}:?\s*"
        r"(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
    )

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # 空行只作为不同 block 之间的分隔
        if not stripped:
            i += 1
            continue

        # =========================
        # 1. fenced code block
        # =========================
        # 必须优先判断代码块。
        # 因为代码内部可能存在：
        # - xxx
        # | xxx |
        # > xxx
        # 如果先判断 list/table 会误识别。
        if stripped.startswith("```") or stripped.startswith("~~~"):
            fence = "```" if stripped.startswith("```") else "~~~"
            block_lines = [line]
            i += 1

            while i < len(lines):
                current_line = lines[i]
                block_lines.append(current_line)
                # 找到结束 fence
                if current_line.strip().startswith(fence):
                    i += 1
                    break
                i += 1

            blocks.append({
                "type": "code",
                "content": "\n".join(block_lines),
            })

            continue

        # =========================
        # 2. table
        # =========================
        # Markdown 表格至少要满足：
        #
        # | name | age |
        # | ---- | --- |
        #
        if (
            i + 1 < len(lines)
            and "|" in line
            and table_separator_pattern.match(lines[i + 1])
        ):
            block_lines = [
                line,
                lines[i + 1],
            ]

            i += 2

            # 后面的连续表格行
            while i < len(lines):
                current_line = lines[i]

                if not current_line.strip():
                    break

                if "|" not in current_line:
                    break

                block_lines.append(current_line)
                i += 1

            blocks.append({
                "type": "table",
                "content": "\n".join(block_lines),
            })

            continue

        # =========================
        # 3. list
        # =========================
        if list_pattern.match(line):
            block_lines = [line]
            i += 1

            while i < len(lines):
                current_line = lines[i]

                if not current_line.strip():
                    break

                # 新的列表项
                if list_pattern.match(current_line):
                    block_lines.append(current_line)
                    i += 1
                    continue

                # 缩进内容视为当前 list item 的补充
                if current_line.startswith((" ", "\t")):
                    block_lines.append(current_line)
                    i += 1
                    continue

                break

            blocks.append({
                "type": "list",
                "content": "\n".join(block_lines),
            })

            continue

        # =========================
        # 4. blockquote
        # =========================
        if stripped.startswith(">"):
            block_lines = [line]
            i += 1

            while i < len(lines):
                current_line = lines[i]
                if not current_line.strip():
                    break
                if not current_line.lstrip().startswith(">"):
                    break

                block_lines.append(current_line)
                i += 1

            blocks.append({
                "type": "blockquote",
                "content": "\n".join(block_lines),
            })

            continue

        # =========================
        # 5. 普通正文 paragraph
        # =========================
        block_lines = [line]
        i += 1

        while i < len(lines):
            current_line = lines[i]
            current_stripped = current_line.strip()

            # 空行意味着当前 paragraph 结束
            if not current_stripped:
                break

            # 后面开始代码块
            if (
                current_stripped.startswith("```")
                or current_stripped.startswith("~~~")
            ):
                break
            # 后面开始列表
            if list_pattern.match(current_line):
                break
            # 后面开始引用
            if current_stripped.startswith(">"):
                break
            # 后面开始表格
            if (
                i + 1 < len(lines)
                and "|" in current_line
                and table_separator_pattern.match(lines[i + 1])
            ):
                break
            block_lines.append(current_line)
            i += 1

        blocks.append({
            "type": "paragraph",
            "content": "\n".join(block_lines),
        })

    return merge_blocks(
        blocks,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )


def split_content(
    content: str,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
):
    # 获取当前 content 的 token 数量
    tokens = count_tokens(content)
    # 比较
    if tokens <= max_tokens:
        return [{"types": ["content"], "content": content}]
    else:
        return split_markdown_blocks(
            content,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )


def render_heading_context(headings: dict[int, str]) -> str:
    """把标题层级重新放回文本，使 Embedding 能看到章节语义。"""
    return "\n".join(
        f"{'#' * level} {headings[level]}"
        for level in sorted(headings)
    )


def save_section(
    content_lines: list,
    chunks: list,
    headings: dict,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
):
    if not content_lines:
        return

    content = "\n".join(content_lines).strip()

    if not content:
        return

    heading_path = " > ".join(headings[level] for level in sorted(headings))
    heading_context = render_heading_context(headings)

    # 标题也会进入最终 content，因此正文拆分时要为标题预留 token。
    context_tokens = count_tokens(heading_context) if heading_context else 0
    body_max_tokens = max(100, max_tokens - context_tokens)
    contents = split_content(
        content,
        max_tokens=body_max_tokens,
        overlap_tokens=min(overlap_tokens, body_max_tokens // 2),
    )

    start_index = len(chunks)
    for i, item in enumerate(contents):
        body = item["content"]
        enriched_content = (
            f"{heading_context}\n\n{body}"
            if heading_context
            else body
        )
        chunks.append({
            "heading_path": heading_path,
            "chunk_index": start_index + i,
            "content": enriched_content,
            "body": body,
            "block_types": item["types"],
            "is_continuation": i > 0,
        })


HEADING_PATTERN = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


def split_markdown(
    text: str,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
):
    """预处理并拆分 Markdown，返回文档元数据和 chunks。"""
    metadata, text = preprocess_markdown(text)
    lines = text.splitlines()

    chunks = []
    headings = {}
    if metadata.get("title"):
        headings[1] = str(metadata["title"]).strip()

    content_lines = []
    active_fence = None

    for line in lines:
        stripped = line.strip()

        # 代码块里的 `# comment` 不是 Markdown 标题。
        if active_fence:
            content_lines.append(line)
            if stripped.startswith(active_fence):
                active_fence = None
            continue

        if stripped.startswith("```") or stripped.startswith("~~~"):
            active_fence = "```" if stripped.startswith("```") else "~~~"
            content_lines.append(line)
            continue

        heading_match = HEADING_PATTERN.match(stripped)
        if heading_match:
            save_section(
                content_lines,
                chunks,
                headings,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
            )
            content_lines = []

            prefix, title = heading_match.groups()
            level = len(prefix)
            headings[level] = title.strip()

            # 新标题出现后清除比它更低级的旧标题。
            for old_level in list(headings):
                if old_level > level:
                    del headings[old_level]
            continue

        content_lines.append(line)

    save_section(
        content_lines,
        chunks,
        headings,
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )
    return metadata, chunks


def load_and_split_markdown(
    file_path: str,
    max_tokens: int = 800,
    overlap_tokens: int = 100,
):
    return split_markdown(
        read_file(file_path),
        max_tokens=max_tokens,
        overlap_tokens=overlap_tokens,
    )



def main():
    project_root = Path(__file__).resolve().parents[1]
    file_path = project_root / "data/knowledge_raw/markdown/kubernetes/debug-init-containers.md"
    metadata, chunks = load_and_split_markdown(str(file_path))

    print(metadata)
    for chunk in chunks:
        print(f"heading_path: {chunk['heading_path']}")
        print(f"chunk_index: {chunk['chunk_index']}")
        print(f"block_types: {chunk['block_types']}")
        print(f"content: {chunk['content']}")
        print("----------------------------------")


if __name__ == "__main__":
    main()

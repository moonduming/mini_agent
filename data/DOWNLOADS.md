# 数据下载清单

本文只记录原始数据下载地址和建议保存位置，不包含清洗、切分、Embedding 或向量入库。

## 推荐组合

第一版项目下载以下数据即可：

1. OpenStack、ZooKeeper、Apache、Linux 完整日志。
2. Kubernetes 的 5 篇排障文档。
3. Kafka 的 2 篇运维文档。

HDFS 和 Hadoop 用于后续大数据量测试，可暂不下载。

## 完整日志

| 数据集 | 下载地址 | 建议保存为 | 说明 |
| --- | --- | --- | --- |
| OpenStack | https://zenodo.org/records/8196385/files/OpenStack.tar.gz?download=1 | `raw_logs/full/openstack/OpenStack.tar.gz` | 主数据集，包含正常及故障注入场景 |
| ZooKeeper | https://zenodo.org/records/8196385/files/Zookeeper.tar.gz?download=1 | `raw_logs/full/zookeeper/Zookeeper.tar.gz` | 分布式协调服务日志 |
| Apache | https://zenodo.org/records/8196385/files/Apache.tar.gz?download=1 | `raw_logs/full/apache/Apache.tar.gz` | Web 服务错误日志 |
| Linux | https://zenodo.org/records/8196385/files/Linux.tar.gz?download=1 | `raw_logs/full/linux/Linux.tar.gz` | Linux 系统日志 |
| HDFS v1（可选） | https://zenodo.org/records/8196385/files/HDFS_v1.zip?download=1 | `raw_logs/full/hdfs/HDFS_v1.zip` | 约 1117 万行，带异常标签，数据量较大 |
| Hadoop（可选） | https://zenodo.org/records/8196385/files/Hadoop.zip?download=1 | `raw_logs/full/hadoop/Hadoop.zip` | MapReduce 作业日志 |

浏览器下载后，把压缩包放到表格中的目录。也可以在项目根目录执行：

```bash
curl -L 'https://zenodo.org/records/8196385/files/OpenStack.tar.gz?download=1' -o data/raw_logs/full/openstack/OpenStack.tar.gz
curl -L 'https://zenodo.org/records/8196385/files/Zookeeper.tar.gz?download=1' -o data/raw_logs/full/zookeeper/Zookeeper.tar.gz
curl -L 'https://zenodo.org/records/8196385/files/Apache.tar.gz?download=1' -o data/raw_logs/full/apache/Apache.tar.gz
curl -L 'https://zenodo.org/records/8196385/files/Linux.tar.gz?download=1' -o data/raw_logs/full/linux/Linux.tar.gz
```

下载完成后分别在对应目录解压。数据来源及使用要求见 LogHub：

- https://github.com/logpai/loghub
- https://zenodo.org/records/8196385

## Kubernetes 知识库（原始 Markdown）

| 文档 | 下载地址 | 建议保存为 |
| --- | --- | --- |
| Pod 排障 | https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-pods.md | `knowledge_raw/markdown/kubernetes/debug-pods.md` |
| Service 排障 | https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-service.md | `knowledge_raw/markdown/kubernetes/debug-service.md` |
| 运行中 Pod 排障 | https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-running-pod.md | `knowledge_raw/markdown/kubernetes/debug-running-pod.md` |
| 判断 Pod 失败原因 | https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md | `knowledge_raw/markdown/kubernetes/determine-reason-pod-failure.md` |
| Init Container 排障 | https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-init-containers.md | `knowledge_raw/markdown/kubernetes/debug-init-containers.md` |

```bash
curl -L 'https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-pods.md' -o data/knowledge_raw/markdown/kubernetes/debug-pods.md
curl -L 'https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-service.md' -o data/knowledge_raw/markdown/kubernetes/debug-service.md
curl -L 'https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-running-pod.md' -o data/knowledge_raw/markdown/kubernetes/debug-running-pod.md
curl -L 'https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/determine-reason-pod-failure.md' -o data/knowledge_raw/markdown/kubernetes/determine-reason-pod-failure.md
curl -L 'https://raw.githubusercontent.com/kubernetes/website/main/content/en/docs/tasks/debug/debug-application/debug-init-containers.md' -o data/knowledge_raw/markdown/kubernetes/debug-init-containers.md
```

## Kafka 知识库（原始 Markdown）

| 文档 | 下载地址 | 建议保存为 |
| --- | --- | --- |
| 基础集群运维 | https://raw.githubusercontent.com/apache/kafka/trunk/docs/operations/basic-kafka-operations.md | `knowledge_raw/markdown/kafka/basic-kafka-operations.md` |
| KRaft 运维 | https://raw.githubusercontent.com/apache/kafka/trunk/docs/operations/kraft.md | `knowledge_raw/markdown/kafka/kraft.md` |

```bash
curl -L 'https://raw.githubusercontent.com/apache/kafka/trunk/docs/operations/basic-kafka-operations.md' -o data/knowledge_raw/markdown/kafka/basic-kafka-operations.md
curl -L 'https://raw.githubusercontent.com/apache/kafka/trunk/docs/operations/kraft.md' -o data/knowledge_raw/markdown/kafka/kraft.md
```

## 目录说明

- `raw_logs/*.log`：现有 2k 行样本，继续用于快速调试和单元测试。
- `raw_logs/full/`：自行下载的完整日志压缩包及解压内容。
- `knowledge_raw/*.html`：此前下载的网页版本，暂时保留，不建议直接入库。
- `knowledge_raw/markdown/`：干净的官方 Markdown，作为首批 RAG 原始文档。
- `eval/`：后续放人工编写的测试问题和标准答案。


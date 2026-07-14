#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_ROOT="$HERE/_local_full_backup"

sync_dir() {
  local source_dir="$1"
  local target_name="$2"
  echo "同步：$target_name"
  mkdir -p "$BACKUP_ROOT/$target_name"
  rsync -a --delete "$source_dir/" "$BACKUP_ROOT/$target_name/"
}

mkdir -p "$BACKUP_ROOT"

sync_dir "/Users/works14/.claude/监控/wechat-ybxhyyh-top3" "wechat-search-monitor"
sync_dir "/Users/works14/Documents/zkcode/250626_mpGUI" "wechat-mp-monitor"
sync_dir "/Users/works14/Documents/zkcode/取数/xhs-keyword-monitor" "xhs-keyword-monitor"
sync_dir "/Users/works14/Documents/zkcode/GEOProMax" "geopromax"
sync_dir "/Users/works14/Documents/output_md/wiki-viewer" "wiki-viewer"
sync_dir "/Users/works14/Documents/output_md/wiki-viewer/WritingMoney" "writing-money"
sync_dir "/Users/works14/Documents/zkcode/YZKcode/1126WritePublish" "wechat-publish-system"
sync_dir "/Users/works14/Documents/output_md" "mother-article-library"

# 原项目这里是指向 ~/.skills-manager 的绝对软链接。将链接目标复制成实体，
# 避免原路径或 Skill 被删除后，完整备份中的微信搜索脚本失效。
rm -f "$BACKUP_ROOT/wechat-search-monitor/source/scripts"
mkdir -p "$BACKUP_ROOT/wechat-search-monitor/source/scripts"
rsync -a --delete \
  "/Users/works14/.skills-manager/skills/zk-wechat-search/scripts/" \
  "$BACKUP_ROOT/wechat-search-monitor/source/scripts/"

date '+同步完成：%Y-%m-%d %H:%M:%S %z' > "$BACKUP_ROOT/LAST_SYNC.txt"
du -sh "$BACKUP_ROOT" "$BACKUP_ROOT"/*

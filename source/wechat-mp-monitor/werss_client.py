import requests
import json
import time
from datetime import datetime, date
from typing import List, Dict, Any, Optional
from urllib.parse import quote, urljoin

class WeRSSClient:
    def __init__(self, base_url: str = "http://192.168.31.89:8001", username: str = None, password: str = None):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.token = None
        
        if username and password:
            self.login(username, password)
    
    def login(self, username: str, password: str) -> bool:
        """用户登录获取token"""
        login_data = {
            "username": username,
            "password": password,
            "grant_type": "password"
        }
        
        try:
            response = self.session.post(
                f"{self.base_url}/api/v1/wx/auth/token",
                data=login_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get("access_token"):
                    self.token = result["access_token"]
                    self.session.headers.update({
                        "Authorization": f"Bearer {self.token}"
                    })
                    print("登录成功！")
                    return True
            
            print(f"登录失败: {response.text}")
            return False
            
        except Exception as e:
            print(f"登录错误: {e}")
            return False
    
    def get_all_mps(self, status: Optional[int] = None, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取所有公众号列表（支持分页）"""
        try:
            all_mps = []
            offset = 0
            limit = 100
            
            while True:
                params = {"limit": limit, "offset": offset}
                if status is not None:
                    params["status"] = status
                if keyword:
                    params["kw"] = keyword

                response = self.session.get(
                    f"{self.base_url}/api/v1/wx/mps",
                    params=params
                )
                
                if response.status_code != 200:
                    print(f"获取公众号列表失败 (offset={offset}): {response.text}")
                    break
                
                result = response.json()
                data = result.get("data", {})
                
                # 获取当前页的公众号
                if isinstance(data, dict):
                    mps = data.get("list", [])
                    total = data.get("total", 0)
                else:
                    mps = data if isinstance(data, list) else []
                    total = len(mps)
                
                if not mps:
                    break
                
                all_mps.extend(mps)
                
                # 如果已获取所有公众号，退出循环
                if len(all_mps) >= total or len(mps) < limit:
                    break
                
                offset += limit
            
            return all_mps
                
        except Exception as e:
            print(f"获取公众号列表错误: {e}")
            return []
    
    def get_qr_status(self) -> Dict[str, Any]:
        """获取微信扫码登录状态"""
        try:
            response = self.session.get(f"{self.base_url}/api/v1/wx/auth/qr/status")
            if response.status_code == 200:
                result = response.json()
                return result.get("data", {}) if isinstance(result, dict) else {}
            print(f"获取微信扫码状态失败: {response.text}")
            return {}
        except Exception as e:
            print(f"获取微信扫码状态错误: {e}")
            return {}

    def get_qr_code(self) -> Dict[str, Any]:
        """生成微信登录二维码并返回二维码地址"""
        try:
            response = self.session.get(f"{self.base_url}/api/v1/wx/auth/qr/code")
            payload = self._safe_json(response)
            if response.status_code != 200:
                raise RuntimeError(self._extract_error_message(payload, response.text))

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            if not isinstance(data, dict):
                raise RuntimeError("二维码返回格式异常")

            code_url = str(data.get("code") or "").strip()
            if not code_url:
                raise RuntimeError("二维码地址为空")

            data["absolute_code_url"] = self.resolve_url(code_url)
            return data
        except Exception as e:
            raise RuntimeError(f"获取二维码失败：{e}") from e

    def finish_qr_login(self) -> bool:
        """通知服务端扫码流程已结束"""
        try:
            response = self.session.get(f"{self.base_url}/api/v1/wx/auth/qr/over")
            return response.status_code == 200
        except Exception:
            return False

    def search_mps(self, keyword: str, limit: int = 1, offset: int = 0) -> Dict[str, Any]:
        """搜索公众号，用于强校验微信登录是否真实可用"""
        if not keyword.strip():
            raise ValueError("搜索关键词不能为空")

        encoded_keyword = quote(keyword.strip(), safe="")
        try:
            response = self.session.get(
                f"{self.base_url}/api/v1/wx/mps/search/{encoded_keyword}",
                params={"limit": limit, "offset": offset},
            )
            payload = self._safe_json(response)
            if response.status_code == 200:
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                return {
                    "ok": True,
                    "status_code": response.status_code,
                    "message": str(payload.get("message", "success")) if isinstance(payload, dict) else "success",
                    "data": data if isinstance(data, dict) else {},
                }

            return {
                "ok": False,
                "status_code": response.status_code,
                "message": self._extract_error_message(payload, response.text),
                "data": None,
            }
        except Exception as e:
            return {
                "ok": False,
                "status_code": 0,
                "message": f"搜索公众号请求失败：{e}",
                "data": None,
            }

    def check_wechat_login(self, keyword: str) -> Dict[str, Any]:
        """通过真实业务接口强校验微信登录状态"""
        result = self.search_mps(keyword=keyword, limit=1, offset=0)
        if result.get("ok"):
            data = result.get("data") or {}
            return {
                "logged_in": True,
                "can_confirm": True,
                "keyword": keyword,
                "message": f"强校验通过：搜索接口可正常访问（关键词：{keyword}）",
                "total": int(data.get("total", 0) or 0),
            }

        message = str(result.get("message", "") or "强校验失败")
        auth_error_markers = ("重新扫码授权", "请先扫码", "未登录", "授权失效")
        return {
            "logged_in": False,
            "can_confirm": any(marker in message for marker in auth_error_markers),
            "keyword": keyword,
            "message": message,
            "total": 0,
        }

    def download_binary(self, url_or_path: str) -> bytes:
        """下载二维码图片等二进制内容"""
        response = self.session.get(self.resolve_url(url_or_path))
        if response.status_code == 200 and response.content:
            return response.content
        raise RuntimeError(f"下载内容失败：HTTP {response.status_code}")

    def wait_for_qr_code_image(
        self,
        url_or_path: str,
        timeout_seconds: int = 15,
        poll_interval: float = 1.0,
    ) -> bytes:
        """等待二维码图片生成完成后返回图片内容"""
        deadline = time.time() + max(timeout_seconds, 1)
        last_error = "二维码图片尚未生成"

        while time.time() < deadline:
            try:
                data = self.download_binary(url_or_path)
                if data:
                    return data
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(poll_interval, 0.1))

        raise RuntimeError(last_error)

    def resolve_url(self, url_or_path: str) -> str:
        """把相对路径补成完整 URL"""
        value = str(url_or_path or "").strip()
        if value.startswith("http://") or value.startswith("https://"):
            return value
        return urljoin(f"{self.base_url}/", value.lstrip("/"))

    def update_mp_articles(
        self,
        mp_id: str,
        start_page: Optional[int] = None,
        end_page: Optional[int] = None,
    ) -> bool:
        """更新指定公众号的文章"""
        try:
            params = {}
            if start_page is not None:
                params["start_page"] = start_page
            if end_page is not None:
                params["end_page"] = end_page

            response = self.session.get(
                f"{self.base_url}/api/v1/wx/mps/update/{mp_id}",
                params=params or None,
            )
            
            if response.status_code == 200:
                print(f"公众号 {mp_id} 文章更新成功")
                return True
            else:
                print(f"公众号 {mp_id} 文章更新失败: {response.text}")
                return False
                
        except Exception as e:
            print(f"更新公众号 {mp_id} 文章错误: {e}")
            return False

    def _safe_json(self, response: requests.Response) -> Dict[str, Any]:
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    def _extract_error_message(self, payload: Dict[str, Any], fallback: str = "") -> str:
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict) and detail.get("message"):
                return str(detail["message"])
            if payload.get("message"):
                return str(payload["message"])

        fallback_text = str(fallback or "").strip()
        return fallback_text or "请求失败"
    
    def get_mp_articles(self, mp_id: str) -> List[Dict[str, Any]]:
        """获取指定公众号的所有文章（支持分页）"""
        try:
            all_articles = []
            offset = 0
            limit = 100  # 每页100篇
            
            while True:
                params = {
                    "limit": limit,
                    "offset": offset,
                    "mp_id": mp_id
                }
                
                response = self.session.get(
                    f"{self.base_url}/api/v1/wx/articles",
                    params=params
                )
                
                if response.status_code != 200:
                    print(f"获取公众号 {mp_id} 文章列表失败 (offset={offset}): {response.text}")
                    break
                
                result = response.json()
                data = result.get("data", {})
                
                # 获取当前页的文章
                if isinstance(data, dict):
                    articles = data.get("list", [])
                    total = data.get("total", 0)
                else:
                    articles = data if isinstance(data, list) else []
                    total = len(articles)
                
                if not articles:
                    break
                
                all_articles.extend(articles)
                
                # 如果已获取所有文章，退出循环
                if len(all_articles) >= total or len(articles) < limit:
                    break
                
                offset += limit
                
            return all_articles
                
        except Exception as e:
            print(f"获取公众号 {mp_id} 文章错误: {e}")
            return []
    
    def get_articles_by_date(self, target_date: str, mp_id: str = None) -> List[Dict[str, Any]]:
        """获取指定日期的文章（支持分页获取所有文章）"""
        try:
            all_articles = []
            offset = 0
            limit = 100  # 每页100篇（API最大限制）
            
            while True:
                params = {
                    "limit": limit,
                    "offset": offset
                }
                
                if mp_id:
                    params["mp_id"] = mp_id
                
                response = self.session.get(
                    f"{self.base_url}/api/v1/wx/articles",
                    params=params
                )
                
                if response.status_code != 200:
                    print(f"获取文章列表失败 (offset={offset}): {response.text}")
                    break
                
                result = response.json()
                data = result.get("data", {})
                
                # 获取当前页的文章
                if isinstance(data, dict):
                    articles = data.get("list", [])
                    total = data.get("total", 0)
                else:
                    articles = data if isinstance(data, list) else []
                    total = len(articles)
                
                if not articles:
                    break
                
                all_articles.extend(articles)
                print(f"   已获取 {len(all_articles)} 篇文章...")
                
                # 检查是否还有更多数据 - 修复关键逻辑
                if total > 0 and len(all_articles) >= total:
                    # 已获取所有文章
                    break
                elif len(articles) == 0:
                    # 当前页无数据
                    break
                else:
                    # 继续获取下一页
                    offset += limit
            
            print(f"   总共获取到 {len(all_articles)} 篇文章")
            
            # 过滤指定日期的文章
            target_articles = []
            for article in all_articles:
                publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
                if publish_time:
                    try:
                        if isinstance(publish_time, (int, float)):
                            article_date = datetime.fromtimestamp(publish_time).date()
                        elif isinstance(publish_time, str):
                            if "T" in publish_time:
                                article_date = datetime.fromisoformat(publish_time.replace("Z", "+00:00")).date()
                            else:
                                article_date = datetime.strptime(publish_time[:10], "%Y-%m-%d").date()
                        else:
                            continue
                        
                        target_date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
                        
                        if article_date == target_date_obj:
                            target_articles.append(article)
                    except Exception as date_error:
                        continue
            
            print(f"   过滤后 {target_date} 的文章: {len(target_articles)} 篇")
            return target_articles
            
        except Exception as e:
            print(f"获取文章错误: {e}")
            return []
    
    def get_mp_info(self, mp_id: str) -> Dict[str, Any]:
        """获取公众号详细信息"""
        try:
            response = self.session.get(
                f"{self.base_url}/api/v1/wx/mps/{mp_id}"
            )
            
            if response.status_code == 200:
                result = response.json()
                return result.get("data", {})
            else:
                print(f"获取公众号 {mp_id} 信息失败: {response.text}")
                return {}
                
        except Exception as e:
            print(f"获取公众号 {mp_id} 信息错误: {e}")
            return {}

    def update_mp_status(self, mp_id: str, status: int) -> bool:
        """更新公众号启用状态"""
        try:
            response = self.session.put(
                f"{self.base_url}/api/v1/wx/mps/{mp_id}",
                json={"status": status},
            )
            if response.status_code == 200:
                return True
            print(f"更新公众号 {mp_id} 状态失败: {response.text}")
            return False
        except Exception as e:
            print(f"更新公众号 {mp_id} 状态错误: {e}")
            return False


def main():
    # 初始化客户端 - 请根据实际情况修改用户名和密码
    client = WeRSSClient(
        base_url="http://192.168.31.89:8001",
        username="admin",  # 请替换为实际用户名
        password="admin@123"   # 请替换为实际密码
    )
    
    if not client.token:
        print("登录失败，请检查用户名和密码")
        return
    
    print("=" * 50)
    print("第一步：更新所有公众号文章")
    print("=" * 50)
    
    # 获取所有公众号
    mps = client.get_all_mps()
    if not mps:
        print("未找到任何公众号")
        return
    
    print(f"找到 {len(mps)} 个公众号，开始更新...")
    
    # 更新所有公众号的文章
    for mp in mps:
        mp_id = mp.get("mp_id") or mp.get("id")
        mp_name = mp.get("mp_name") or mp.get("name", "未知")
        
        if mp_id:
            print(f"正在更新公众号: {mp_name} (ID: {mp_id})")
            client.update_mp_articles(mp_id)
        else:
            print(f"公众号 {mp_name} 缺少ID，跳过更新")
    
    print("\n" + "=" * 50)
    print("第二步：读取2025年6月21日发布的文章")
    print("=" * 50)
    
    target_date = "2025-06-21"
    
    # 获取指定日期的所有文章
    articles = client.get_articles_by_date(target_date)
    
    if not articles:
        print(f"未找到 {target_date} 发布的文章")
        return
    
    print(f"找到 {len(articles)} 篇 {target_date} 发布的文章：")
    print("\n" + "=" * 50)
    
    # 打印文章信息
    for i, article in enumerate(articles, 1):
        print(f"文章 {i}:")
        print("-" * 30)
        
        # 文章基本信息
        title = article.get("title", "无标题")
        url = article.get("url") or article.get("link") or article.get("content_url", "无链接")
        mp_id = article.get("mp_id") or article.get("account_id")
        
        print(f"标题: {title}")
        print(f"文章URL: {url}")
        
        # 获取公众号信息
        if mp_id:
            mp_info = client.get_mp_info(mp_id)
            if mp_info:
                mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
                mp_intro = mp_info.get("mp_intro") or mp_info.get("intro", "")
                avatar = mp_info.get("avatar", "")
                
                print(f"公众号名称: {mp_name}")
                print(f"公众号ID: {mp_id}")
                if mp_intro:
                    print(f"公众号简介: {mp_intro}")
                if avatar:
                    print(f"公众号头像: {avatar}")
            else:
                print(f"公众号ID: {mp_id} (无法获取详细信息)")
        else:
            print("公众号信息: 未知")
        
        # 其他文章信息
        author = article.get("author", "")
        if author:
            print(f"作者: {author}")
        
        publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
        if publish_time:
            print(f"发布时间: {publish_time}")
        
        print("\n")


if __name__ == "__main__":
    main() 

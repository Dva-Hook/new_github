# -*- coding: utf-8 -*-
"""V7 W1 步骤 - 使用 RuyiPage 独立登录并创建 WoW Trial 账号"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from ruyipage import FirefoxPage, FirefoxOptions


LOG = logging.getLogger(__name__)


# 创建 WoW Trial 账号的 JavaScript 脚本
CREATE_WOW_TRIAL_SCRIPT = """
(async () => {
  const xsrf = (document.cookie.match(/(?:^|;\\s*)XSRF-TOKEN=([^;]+)/) || [])[1];
  if (!xsrf) {
    console.error('❌ 未找到 XSRF-TOKEN');
    return { error: 'XSRF_TOKEN_NOT_FOUND' };
  }
  console.log('✅ 获取到 XSRF-TOKEN');

  const body = JSON.stringify({ region: 'US' });
  console.log('📤 POST /api/game-account/creation/wow-trial');

  const r1 = await fetch('https://account.battle.net/api/game-account/creation/wow-trial', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-XSRF-TOKEN': xsrf,
    },
    body: body,
  });

  console.log('↩️ 创建试玩账号响应状态:', r1.status);
  let d1 = null;
  try { d1 = await r1.json(); } catch (e) { d1 = await r1.text().catch(() => null); }
  console.log('📥 响应内容:', d1);

  console.log('📤 GET /api/games-and-subs');
  const r2 = await fetch('https://account.battle.net/api/games-and-subs', {
    method: 'GET',
    credentials: 'include',
    headers: {
      'Accept': '*/*',
      'X-XSRF-TOKEN': xsrf,
    },
  });

  console.log('↩️ 游戏订阅响应状态:', r2.status);
  let d2 = null;
  try { d2 = await r2.json(); } catch (e) { d2 = await r2.text().catch(() => null); }
  console.log('📥 响应内容:', d2);

  return { createStatus: r1.status, createBody: d1, subsStatus: r2.status, subsBody: d2 };
})();
"""


# 验证 WoW Trial 账号的 JavaScript 脚本
VERIFY_WOW_TRIAL_SCRIPT = """
(async () => {
  const xsrf = (document.cookie.match(/(?:^|;\\s*)XSRF-TOKEN=([^;]+)/) || [])[1];
  if (!xsrf) {
    console.error('❌ 未找到 XSRF-TOKEN');
    return { error: 'XSRF_TOKEN_NOT_FOUND' };
  }

  const r = await fetch('https://account.battle.net/api/games-and-subs', {
    method: 'GET',
    credentials: 'include',
    headers: { 'Accept': '*/*', 'X-XSRF-TOKEN': xsrf },
  });

  console.log('↩️ 状态:', r.status);
  const data = await r.json();

  const accounts = data?.gameAccounts ?? data ?? [];
  console.log(`📥 共 ${accounts.length} 个游戏子账号`);

  accounts.forEach((a, i) => {
    console.log(`  [${i}] ${a.gameAccountName} - titleId=${a.titleId}, status=${a.gameAccountStatus}, region=${a.gameAccountRegion}`);
  });

  return { status: r.status, accounts: accounts };
})();
"""


def create_w1_browser(
    proxy: str = "",
    user_dir: Optional[Path] = None,
    headless: bool = False,
) -> FirefoxPage:
    """
    创建 V7 W1 步骤专用的 RuyiPage 浏览器

    Args:
        proxy: 代理地址（例如 http://127.0.0.1:7890）
        user_dir: 用户数据目录（None 使用临时目录）
        headless: 是否无头模式

    Returns:
        FirefoxPage: RuyiPage 浏览器实例
    """
    opts = FirefoxOptions()

    # 设置用户数据目录
    if user_dir:
        opts.set_user_dir(str(user_dir))

    # 设置代理
    if proxy:
        opts.set_proxy(proxy)

    # 设置窗口大小
    opts.set_window_size(1440, 900)

    # 无头模式
    if headless:
        opts.set_argument("--headless")

    # 创建浏览器实例
    page = FirefoxPage(opts)

    LOG.info(f"V7 W1 浏览器已创建: headless={headless}, proxy={bool(proxy)}")

    return page


def login_battle_net(page: FirefoxPage, email: str, password: str) -> bool:
    """
    在 RuyiPage 浏览器中登录战网（分两步：先邮箱，后密码）
    
    参考 V6 验证邮箱时的登录逻辑，战网登录是分步骤的：
    1. 输入邮箱，点击提交
    2. 等待密码输入框出现
    3. 输入密码，点击提交
    4. 等待登录完成
    
    Args:
        page: RuyiPage 浏览器实例
        email: 账号邮箱
        password: 账号密码
    
    Returns:
        bool: 登录是否成功
    """
    try:
        LOG.info(f"V7 W1 开始登录战网: {email}")
        
        # 步骤 1: 打开战网登录页面
        page.get("https://account.battle.net/login")
        time.sleep(2)
        
        # 步骤 2: 输入邮箱
        email_input = page.ele("#accountName", timeout=10)
        if not email_input:
            LOG.error("V7 W1 未找到邮箱输入框")
            return False
        
        email_input.clear()
        email_input.input(email)
        time.sleep(0.5)
        
        # 步骤 3: 点击提交按钮（提交邮箱）
        submit_button = page.ele("#submit", timeout=10)
        if not submit_button:
            LOG.error("V7 W1 未找到提交按钮")
            return False
        
        submit_button.click()
        LOG.info("V7 W1 已提交邮箱，等待密码输入框出现")
        
        # 步骤 4: 等待密码输入框出现（最多 30 秒）
        password_input = None
        for i in range(30):
            time.sleep(1)
            password_input = page.ele("#password")
            if password_input:
                LOG.info("V7 W1 密码输入框已出现")
                break
            
            # 检查是否已经登录成功（可能账号已保存密码）
            current_url = page.url
            if "account.battle.net/overview" in current_url or "account.battle.net/games" in current_url:
                LOG.info(f"V7 W1 已自动登录成功: {current_url}")
                return True
        
        if not password_input:
            LOG.error("V7 W1 未找到密码输入框（超时 30 秒）")
            return False
        
        # 步骤 5: 输入密码
        password_input.clear()
        password_input.input(password)
        time.sleep(0.5)
        
        # 步骤 6: 点击提交按钮（提交密码）
        submit_button = page.ele("#submit", timeout=10)
        if not submit_button:
            LOG.error("V7 W1 未找到提交按钮（密码步骤）")
            return False
        
        submit_button.click()
        LOG.info("V7 W1 已提交密码，等待登录完成")
        
        # 步骤 7: 等待登录完成（最多 30 秒）
        for i in range(30):
            time.sleep(1)
            current_url = page.url
            
            # 检查是否已经跳转到账号管理页面
            if "account.battle.net/overview" in current_url or "account.battle.net/games" in current_url:
                LOG.info(f"V7 W1 登录成功: {current_url}")
                return True
            
            # 检查是否有错误提示
            error_elem = page.ele("css:.error-message")
            if error_elem and error_elem.text:
                LOG.error(f"V7 W1 登录失败: {error_elem.text}")
                return False
        
        LOG.warning("V7 W1 登录超时（30秒）")
        return False
    
    except Exception as e:
        LOG.exception(f"V7 W1 登录异常: {e}")
        return False


def execute_w1_step_with_login(
    email: str,
    password: str,
    proxy: str = "",
    headless: bool = False,
    user_dir: Optional[Path] = None,
) -> bool:
    """
    V7 W1 步骤完整流程：新开浏览器 → 登录 → 创建 WoW Trial

    Args:
        email: 注册成功的账号邮箱
        password: 账号密码
        proxy: 代理地址
        headless: 是否无头模式
        user_dir: 用户数据目录

    Returns:
        bool: WoW Trial 账号是否创建成功
    """
    page = None

    try:
        LOG.info("=" * 60)
        LOG.info(f"V7 W1 步骤开始: {email}")
        LOG.info("=" * 60)

        # 1. 创建浏览器
        LOG.info("V7 W1 创建 RuyiPage 浏览器")
        page = create_w1_browser(proxy=proxy, user_dir=user_dir, headless=headless)

        # 2. 登录战网
        login_success = login_battle_net(page, email, password)

        if not login_success:
            LOG.error("V7 W1 ❌ 登录失败，W1 步骤终止")
            return False

        # 3. 打开游戏账号创建页面
        LOG.info("V7 W1 打开游戏账号创建页面")
        page.get("https://account.battle.net/games#game-account-creation")
        time.sleep(3)

        # 4. 注入创建 WoW Trial 账号的脚本
        LOG.info("V7 W1 注入创建 WoW Trial 账号脚本")
        create_result = page.run_js(CREATE_WOW_TRIAL_SCRIPT)

        if not create_result:
            LOG.error("V7 W1 创建脚本返回空结果")
            return False

        if isinstance(create_result, dict) and "error" in create_result:
            LOG.error(f"V7 W1 创建失败: {create_result['error']}")
            return False

        LOG.info(
            f"V7 W1 创建脚本执行: "
            f"createStatus={create_result.get('createStatus')}, "
            f"subsStatus={create_result.get('subsStatus')}"
        )

        # 等待创建完成
        time.sleep(2)

        # 5. 验证 WoW Trial 账号是否创建成功
        LOG.info("V7 W1 验证 WoW Trial 账号")
        verify_result = page.run_js(VERIFY_WOW_TRIAL_SCRIPT)

        if not verify_result:
            LOG.error("V7 W1 验证脚本返回空结果")
            return False

        if isinstance(verify_result, dict) and "error" in verify_result:
            LOG.error(f"V7 W1 验证失败: {verify_result['error']}")
            return False

        # 6. 判断是否成功创建 WoW Trial 账号
        accounts = verify_result.get("accounts", [])

        if not accounts:
            LOG.warning("V7 W1 未找到任何游戏账号")
            return False

        # 检查是否有 WoW Trial 账号
        for account in accounts:
            title_id = account.get("titleId")
            status = account.get("gameAccountStatus")
            region = account.get("gameAccountRegion")
            name = account.get("gameAccountName", "Unknown")

            LOG.info(
                f"V7 W1 检查账号: {name} - "
                f"titleId={title_id}, status={status}, region={region}"
            )

            # 判断标准：魔兽世界 + Trial + 美服
            if (
                title_id == 5730135  # 魔兽世界
                and status == "Trial"
                and region == "US"
            ):
                LOG.info(f"V7 W1 ✅ WoW Trial 账号创建成功: {name}")
                LOG.info("=" * 60)
                return True

        LOG.warning("V7 W1 未找到符合条件的 WoW Trial 账号")
        LOG.info(f"V7 W1 当前账号列表: {len(accounts)} 个")
        for i, account in enumerate(accounts):
            LOG.info(
                f"  账号 {i}: {account.get('gameAccountName')} - "
                f"titleId={account.get('titleId')}, "
                f"status={account.get('gameAccountStatus')}"
            )

        return False

    except Exception as e:
        LOG.exception(f"V7 W1 步骤执行失败: {e}")
        return False

    finally:
        # 关闭浏览器
        if page:
            try:
                LOG.info("V7 W1 关闭浏览器")
                page.quit()
            except Exception as e:
                LOG.warning(f"V7 W1 关闭浏览器失败: {e}")

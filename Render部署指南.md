# Render.com 部署指南

## 第一步：注册 GitHub 账号（如已有可跳过）
1. 打开 https://github.com
2. 注册账号

## 第二步：创建 GitHub 仓库并上传代码
1. 在 GitHub 点右上角 "+" → "New repository"
2. 仓库名填 `video-download-api`，选 Private（私有）
3. 点 "Create repository"
4. 在你电脑上执行以下命令上传代码：

```bash
cd "D:\庄梓雄\各平台无水印视频下载神器\Douyin_TikTok_Download_API-main"
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/你的用户名/video-download-api.git
git push -u origin main
```

## 第三步：注册 Render.com 账号
1. 打开 https://render.com
2. 点 "Get Started" → 选 "Sign up with GitHub"（用 GitHub 登录）

## 第四步：部署服务
1. 登录 Render 后，点 "New +" → "Web Service"
2. 选 "Build and deploy from a Git repository" → Next
3. 找到你的 `video-download-api` 仓库 → 点 "Connect"
4. 配置页面填写：
   - **Name**: `video-download-api`（或你喜欢的名字）
   - **Region**: Singapore（选离你近的）
   - **Branch**: main
   - **Root Directory**: 留空
   - **Environment**: Docker
   - **Instance Type**: Free
5. 点 "Create Web Service"

## 第五步：等待部署完成
- Render 会自动构建 Docker 镜像并部署
- 大约需要 5-10 分钟
- 部署成功后会显示绿色 "Live" 状态
- 你会得到一个地址，类似：`https://video-download-api.onrender.com`

## 第六步：配置小程序
1. 打开小程序的 `app.js`
2. 找到 `VIDEO_API_BASE` 配置项
3. 改成你的 Render 地址：
```javascript
VIDEO_API_BASE: 'https://video-download-api.onrender.com',  // 改成你的实际地址
```

## 第七步：配置小程序域名白名单
1. 登录微信公众平台：https://mp.weixin.qq.com
2. 进入 "开发" → "开发管理" → "开发设置" → "服务器域名"
3. 在 "request 合法域名" 中添加：`https://video-download-api.onrender.com`
4. 在 "downloadFile 合法域名" 中也添加：`https://video-download-api.onrender.com`

## 注意事项
- 免费服务 15 分钟无人访问会休眠，下次访问需等 30 秒唤醒
- 每月有 750 小时免费额度，个人使用完全够
- 如遇到解析失败，可能是抖音等平台接口变动，需要更新后端代码

## 测试 API
部署完成后，在浏览器打开你的地址加 /docs：
```
https://你的地址.onrender.com/docs
```
能看到 API 文档页面就说明部署成功。

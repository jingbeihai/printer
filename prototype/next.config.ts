import type { NextConfig } from 'next';

// 桌面 EXE 与 Python 云端服务都只需要静态前端资源；业务数据统一走同源 /api/v1。
// 导出静态产物后，WebView 不依赖 Node.js 或另一个网页服务器。
const nextConfig: NextConfig = { output: 'export' };

export default nextConfig;

import type { Metadata } from 'next';
import './globals.css';
export const metadata: Metadata = {
  title: '绍兴数知 · 印务台',
  icons: { icon: '/desk/favicon.svg' },
  description: '学校文印任务、学校预设和打印统计管理',
};
export default function RootLayout({children}:{children:React.ReactNode}) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}

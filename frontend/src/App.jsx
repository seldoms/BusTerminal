import React, { useEffect, useState } from 'react';
import { BrowserRouter as Router, Routes, Route, Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import './index.css';
import './App.css';
import Navbar from './components/Navbar';
import HomePage from './pages/HomePage';
import CategoryPage from './pages/CategoryPage';
import MovieDetailPage from './pages/MovieDetailPage';
import DownloadsPage from './pages/DownloadsPage';
import SubscriptionsPage from './pages/SubscriptionsPage';
import SettingsPage from './pages/SettingsPage';
import SearchPage from './pages/SearchPage';
import NavigationPage from './pages/NavigationPage';
import ActressesPage from './pages/ActressesPage';
import ActressDetailPage from './pages/ActressDetailPage';
import JableHotPage from './pages/JableHotPage';
import DiscoveryPage from './pages/DiscoveryPage';
import LibraryPage from './pages/LibraryPage';
import CandidatesPage from './pages/CandidatesPage';
import TasksPage from './pages/TasksPage';
import LifecyclePage from './pages/LifecyclePage';
import CleanupPage from './pages/CleanupPage';
import LoginPage from './pages/LoginPage';
import OnboardingPage from './pages/OnboardingPage';
import BIDashboardPage from './pages/BIDashboardPage';
import { ToastProvider } from './components/Toast';
import { MovieDrawerProvider } from './components/MovieDrawerContext';
import MovieContextDrawer from './components/MovieContextDrawer';
import { authApi, onboardingApi, getAuthToken, logSafeError } from './api';
import { scrollToTop } from './utils/listCache';

// 路由切换时回滚到顶部；列表页返回时由 MovieInfiniteList 的缓存恢复覆盖为目标位置
function ScrollToTopOnRouteChange() {
    const { pathname } = useLocation();
    useEffect(() => { scrollToTop(); }, [pathname]);
    return null;
}

// 启动时检查鉴权状态：需要登录且无 token 则跳转登录页；status 不可用时不阻塞。
// /setup 也放行：首次部署还没建管理员账号时 auth_required=false，本来就通；
// 而已建账号的重配场景用户已登录。只有这三种页面不强制跳登录。
const AUTH_EXEMPT_PATHS = ['/login', '/setup'];

function AuthGate({ children }) {
    const navigate = useNavigate();
    const location = useLocation();
    const [ready, setReady] = useState(false);

    useEffect(() => {
        let cancelled = false;
        authApi.getStatus()
            .then((res) => {
                if (cancelled) return;
                if (
                    res?.success
                    && res.data?.auth_required
                    && !getAuthToken()
                    && !AUTH_EXEMPT_PATHS.includes(location.pathname)
                ) {
                    navigate('/login', { replace: true });
                }
            })
            .catch(() => {
                // 后端无鉴权功能或网络错误：不阻塞正常使用
            })
            .finally(() => {
                if (!cancelled) setReady(true);
            });
        return () => { cancelled = true; };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    if (!ready) {
        return (
            <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100vh',
                color: 'var(--primary)',
                fontWeight: 600,
            }}>
                加载中...
            </div>
        );
    }
    return children;
}

function NotFoundPage() {
    return (
        <div style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            minHeight: '60vh',
            textAlign: 'center',
            padding: '40px'
        }}>
            <h1 style={{ fontSize: '72px', margin: '0', color: 'var(--primary)' }}>404</h1>
            <h2 style={{ margin: '20px 0 10px', color: 'var(--text-primary)' }}>页面未找到</h2>
            <p style={{ color: 'var(--text-secondary)', marginBottom: '30px' }}>
                您访问的页面不存在或已被移除
            </p>
            <Link to="/" className="btn btn-primary" style={{
                padding: '12px 24px',
                textDecoration: 'none',
                borderRadius: '8px',
                backgroundColor: 'var(--primary)',
                color: 'white'
            }}>
                返回首页
            </Link>
        </div>
    );
}

// 首次部署强拦截：引导没完成就只放行 /login 与 /setup。
// status 读不到时放行 —— 宁可让人先进去看一眼，也不要在后端异常时把人彻底锁死。
const ONBOARDING_EXEMPT_PATHS = ['/login', '/setup'];

function SetupGate({ children }) {
    const location = useLocation();
    const [ready, setReady] = useState(false);
    const [required, setRequired] = useState(false);

    useEffect(() => {
        let cancelled = false;
        onboardingApi.getStatus()
            .then((res) => {
                if (cancelled) return;
                setRequired(!(res?.success && res.data?.completed));
            })
            .catch((err) => {
                if (cancelled) return;
                logSafeError('Onboarding status check failed', err);
            })
            .finally(() => { if (!cancelled) setReady(true); });
        return () => { cancelled = true; };
    }, []);

    if (!ready) {
        return (
            <div style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                height: '100vh',
                color: 'var(--primary)',
                fontWeight: 600,
            }}>
                加载中...
            </div>
        );
    }

    if (required && !ONBOARDING_EXEMPT_PATHS.includes(location.pathname)) {
        return <Navigate to="/setup" replace />;
    }
    return children;
}

function AppLayout() {
    const location = useLocation();
    const posterRoutes = ['/', '/discovery', '/jable/hot', '/library'];
    const isPosterPage = posterRoutes.includes(location.pathname) || location.pathname.startsWith('/category/');
    // 引导页自己占满全屏，不套导航壳
    const isStandalone = location.pathname === '/setup';

    return (
        <div className="app">
            {!isStandalone && <Navbar />}
            <ScrollToTopOnRouteChange />
            <div className={`app-content${isPosterPage ? ' poster-content' : ''}`}>
                <main className="main-content">
                    <Routes>
                        <Route path="/" element={<HomePage />} />
                        <Route path="/login" element={<LoginPage />} />
                        <Route path="/setup" element={<OnboardingPage />} />
                        <Route path="/navigation" element={<NavigationPage />} />
                        <Route path="/actresses" element={<ActressesPage />} />
                        <Route path="/actress/:actressId" element={<ActressDetailPage />} />
                        <Route path="/jable/hot" element={<JableHotPage />} />
                        <Route path="/discovery" element={<DiscoveryPage />} />
                        <Route path="/category/:type" element={<CategoryPage />} />
                        <Route path="/search" element={<SearchPage />} />
                        <Route path="/movie/*" element={<MovieDetailPage />} />
                        <Route path="/downloads" element={<DownloadsPage />} />
                        <Route path="/subscriptions" element={<SubscriptionsPage />} />
                        <Route path="/candidates" element={<CandidatesPage />} />
                        <Route path="/tasks" element={<TasksPage />} />
                        <Route path="/lifecycle" element={<LifecyclePage />} />
                        <Route path="/cleanup" element={<CleanupPage />} />
                        <Route path="/library" element={<LibraryPage />} />
                        <Route path="/jellyfin" element={<LibraryPage />} />
                        <Route path="/settings" element={<SettingsPage />} />
                        <Route path="/bi" element={<BIDashboardPage />} />
                        <Route path="*" element={<NotFoundPage />} />
                    </Routes>
                </main>
            </div>
            <MovieContextDrawer />
        </div>
    );
}

function App() {
    return (
        <ToastProvider>
            <MovieDrawerProvider>
                <Router>
                    <AuthGate>
                        <SetupGate>
                            <AppLayout />
                        </SetupGate>
                    </AuthGate>
                </Router>
            </MovieDrawerProvider>
        </ToastProvider>
    );
}

export default App;

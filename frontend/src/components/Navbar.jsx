import React, { useState, useEffect, useLayoutEffect, useRef } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import { Activity, Film, Home, Sparkles, Flame, Captions, Monitor, UserRound, LayoutGrid, Bell, Download, Search, Sun, Moon, Columns, Settings, RefreshCw, Server, LogOut, ChevronDown, BarChart3, History } from 'lucide-react';
import {
    CARD_WIDTH_MAX,
    CARD_WIDTH_MIN,
    useLayoutSettings,
} from '../layoutSettings';
import { authApi, getAuthToken, clearAuthToken } from '../api';
import './Navbar.css';

const LAYOUT_MODE_LABELS = {
    compact: '紧凑',
    default: '标准',
    poster: '海报',
};

// 主导航项（浏览组）；管理组在空间不足时收进“更多”下拉
const BASE_NAV_ITEMS = [
    { to: '/', label: '首页', Icon: Home },
    { to: '/discovery', label: '发现', Icon: Sparkles },
    { to: '/jable/hot', label: 'Jable热门', Icon: Flame },
    { to: '/category/sub?name=字幕&url=/genre/sub', label: '字幕', Icon: Captions },
    { to: '/category/hd?name=高清影片&url=/genre/hd', label: '高清', Icon: Monitor },
    { to: '/actresses', label: '女优', Icon: UserRound },
    { to: '/navigation', label: '分类', Icon: LayoutGrid },
];

// 导航空间不足时收进“更多”下拉的管理组入口
const MORE_NAV_ITEMS = [
    { to: '/subscriptions', label: '订阅', Icon: Bell },
    { to: '/downloads', label: '下载', Icon: Download },
    { to: '/tasks', label: '任务', Icon: Activity },
    { to: '/lifecycle', label: '生命周期', Icon: History },
    { to: '/library', label: '媒体库', Icon: Server },
    { to: '/bi', label: '数据看板', Icon: BarChart3 },
];

const getStoredTheme = () => {
    if (typeof window === 'undefined') return null;
    try {
        return window.localStorage.getItem('theme');
    } catch {
        return null;
    }
};

const setStoredTheme = (theme) => {
    if (typeof window === 'undefined') return;
    try {
        window.localStorage.setItem('theme', theme);
    } catch {
        // Theme still applies for the current session when storage is unavailable.
    }
};

const prefersDarkTheme = () => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false;
    try {
        return window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch {
        return false;
    }
};

const setRootTheme = (isDark) => {
    if (typeof document === 'undefined' || !document.documentElement) return;
    document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
};

const setRootGridTransition = (value) => {
    if (typeof document === 'undefined' || !document.documentElement) return;
    document.documentElement.style.setProperty('--grid-transition', value);
};

const requestFrame = (callback) => {
    if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
        return window.requestAnimationFrame(callback);
    }
    return setTimeout(callback, 0);
};

const cancelFrame = (frameId) => {
    if (!frameId) return;
    if (typeof window !== 'undefined' && typeof window.cancelAnimationFrame === 'function') {
        window.cancelAnimationFrame(frameId);
        return;
    }
    clearTimeout(frameId);
};

function Navbar() {
    const navigate = useNavigate();
    const location = useLocation();
    const [isMenuOpen, setIsMenuOpen] = useState(false);
    const [searchQuery, setSearchQuery] = useState('');
    const [isDarkMode, setIsDarkMode] = useState(false);
    const [showLogout, setShowLogout] = useState(false);

    const [isMoreOpen, setIsMoreOpen] = useState(false);
    const [isNavCollapsed, setIsNavCollapsed] = useState(false);
    const navRef = useRef(null);
    const moreMenuRef = useRef(null);
    const moreMeasureRef = useRef(null);

    // 鉴权启用且已登录时显示退出按钮；路由变化时重查 token（登录/退出后及时刷新）
    useEffect(() => {
        let cancelled = false;
        authApi.getStatus()
            .then((res) => {
                if (cancelled) return;
                setShowLogout(Boolean(res?.success && res.data?.auth_required && getAuthToken()));
            })
            .catch(() => {
                if (!cancelled) setShowLogout(false);
            });
        return () => { cancelled = true; };
    }, [location.pathname]);

    const handleLogout = () => {
        clearAuthToken();
        setShowLogout(false);
        navigate('/login');
    };

    // 导航空间不足时把管理组（订阅/下载/任务/媒体库）收进“更多”下拉，空间恢复后自动展开
    useLayoutEffect(() => {
        const nav = navRef.current;
        if (!nav) return undefined;
        let frame = 0;
        const checkFit = () => {
            frame = 0;
            if (window.innerWidth <= 1100) {
                // 移动端走汉堡菜单，管理组保持完整展示
                if (isNavCollapsed) setIsNavCollapsed(false);
                return;
            }
            if (nav.clientWidth === 0) return;
            if (!isNavCollapsed) {
                // +1 兜底子像素抖动
                if (nav.scrollWidth > nav.clientWidth + 1) setIsNavCollapsed(true);
            } else {
                // 隐藏副本即全量导航的真实宽度，能整体放下就展开（-8 缓冲避免来回抖动）
                const fullW = moreMeasureRef.current ? moreMeasureRef.current.offsetWidth : 0;
                if (fullW > 0 && fullW <= nav.clientWidth - 8) {
                    setIsNavCollapsed(false);
                }
            }
        };
        const scheduleCheck = () => {
            if (!frame) frame = requestFrame(checkFit);
        };
        checkFit();
        const observer = new ResizeObserver(scheduleCheck);
        observer.observe(nav);
        return () => {
            observer.disconnect();
            if (frame) cancelFrame(frame);
        };
    }, [isNavCollapsed]);

    // “更多”下拉：点击外部或 Esc 关闭
    useEffect(() => {
        if (!isMoreOpen) return undefined;
        const handlePointerDown = (event) => {
            if (moreMenuRef.current && !moreMenuRef.current.contains(event.target)) {
                setIsMoreOpen(false);
            }
        };
        const handleKeyDown = (event) => {
            if (event.key === 'Escape') setIsMoreOpen(false);
        };
        document.addEventListener('mousedown', handlePointerDown);
        document.addEventListener('keydown', handleKeyDown);
        return () => {
            document.removeEventListener('mousedown', handlePointerDown);
            document.removeEventListener('keydown', handleKeyDown);
        };
    }, [isMoreOpen]);

    // 导航选中态：路径 + 查询参数同时匹配才算激活（字幕/高清这类同路由不同参数的入口靠 query 区分）
    const isNavActive = (to) => {
        const [path, query] = to.split('?');
        if (location.pathname !== path) return false;
        if (!query) return true;
        const target = new URLSearchParams(query);
        const current = new URLSearchParams(location.search);
        for (const [key, value] of target.entries()) {
            if (current.get(key) !== value) return false;
        }
        return true;
    };

    // 当前路由落在管理组内时，“更多”按钮保持高亮
    const isMoreActive = MORE_NAV_ITEMS.some((item) => isNavActive(item.to));

    const [{ cardWidth, layoutMode }, setLayoutSettings] = useLayoutSettings();
    const [isLayoutOpen, setIsLayoutOpen] = useState(false);
    const [isDragging, setIsDragging] = useState(false);
    const [dragCardWidth, setDragCardWidth] = useState(cardWidth);
    const dragFrameRef = useRef(0);

    // Load theme preference from localStorage
    useEffect(() => {
        const savedTheme = getStoredTheme();
        const prefersDark = prefersDarkTheme();
        const shouldBeDark = savedTheme === 'dark' || (!savedTheme && prefersDark);

        setIsDarkMode(shouldBeDark);
        setRootTheme(shouldBeDark);
    }, []);

    // 控制拖动时的transition状态
    useEffect(() => {
        if (isDragging) {
            // 拖动时禁用所有网格的transition以提升性能
            setRootGridTransition('none');
        } else {
            // 拖动结束后恢复transition
            setRootGridTransition('all 0.3s ease');
        }
    }, [isDragging]);

    useEffect(() => {
        if (!isDragging) {
            setDragCardWidth(cardWidth);
        }
    }, [cardWidth, isDragging]);

    useEffect(() => () => {
        if (dragFrameRef.current) {
            cancelFrame(dragFrameRef.current);
        }
    }, []);

    const scheduleCardWidth = (value) => {
        const nextWidth = parseInt(value, 10);
        if (!Number.isFinite(nextWidth)) return;

        setDragCardWidth(nextWidth);
        if (dragFrameRef.current) {
            cancelFrame(dragFrameRef.current);
        }

        dragFrameRef.current = requestFrame(() => {
            setLayoutSettings(
                { cardWidth: nextWidth },
                { persist: false, notify: false, preview: true },
            );
            dragFrameRef.current = 0;
        });
    };

    const commitCardWidth = (value) => {
        const nextWidth = Number.isFinite(parseInt(value, 10)) ? parseInt(value, 10) : dragCardWidth;
        if (dragFrameRef.current) {
            cancelFrame(dragFrameRef.current);
            dragFrameRef.current = 0;
        }
        setDragCardWidth(nextWidth);
        setIsDragging(false);
        setRootGridTransition('all 0.3s ease');
        setLayoutSettings(
            { cardWidth: nextWidth },
            { persist: true, notify: true },
        );
    };

    const handleCardWidthChange = (e) => {
        setIsDragging(true);
        scheduleCardWidth(e.target.value);
    };

    const handleSliderMouseDown = () => {
        setIsDragging(true);
        setRootGridTransition('none');
    };

    const handleSliderMouseUp = (e) => {
        commitCardWidth(e.target.value);
    };

    const handleSearch = (e) => {
        e.preventDefault();
        if (searchQuery.trim()) {
            navigate(`/search?q=${encodeURIComponent(searchQuery)}`);
            setIsMenuOpen(false);
        }
    };

    const toggleTheme = () => {
        const newTheme = !isDarkMode;
        setIsDarkMode(newTheme);

        setRootTheme(newTheme);
        setStoredTheme(newTheme ? 'dark' : 'light');
    };

    const toggleMenu = () => {
        setIsMenuOpen(!isMenuOpen);
    };

    return (
        <header className="navbar-header">
            <div className="container">
                <div className="navbar-content">
                    <Link to="/" className="navbar-logo" onClick={() => setIsMenuOpen(false)}>
                        <span className="logo-icon"><Film size={22} /></span>
                        <span className="logo-text">BusTerminal</span>
                    </Link>

                    {/* Desktop Navigation - Centered via Grid */}
                    <nav ref={navRef} className={`navbar-nav ${isMenuOpen ? 'mobile-visible' : ''}`}>
                        {BASE_NAV_ITEMS.map(({ to, label, Icon }) => (
                            <Link key={to} to={to} className={`nav-item${isNavActive(to) ? ' active' : ''}`} onClick={() => setIsMenuOpen(false)}>
                                <Icon size={14} className="nav-item-icon" /> {label}
                            </Link>
                        ))}
                        {/* 全量导航测量副本：屏幕外不可见，仅供折叠判断量宽 */}
                        <span className="nav-measure-hidden" ref={moreMeasureRef} aria-hidden="true">
                            {BASE_NAV_ITEMS.map(({ to, label, Icon }) => (
                                <span key={to} className="nav-item">
                                    <Icon size={14} className="nav-item-icon" /> {label}
                                </span>
                            ))}
                            <span className="nav-divider" />
                            {MORE_NAV_ITEMS.map(({ to, label, Icon }) => (
                                <span key={to} className="nav-item">
                                    <Icon size={14} className="nav-item-icon" /> {label}
                                </span>
                            ))}
                        </span>
                        {!isNavCollapsed && (
                            <>
                                <span className="nav-divider" aria-hidden="true" />
                                {MORE_NAV_ITEMS.map(({ to, label, Icon }) => (
                                    <Link key={to} to={to} className={`nav-item${isNavActive(to) ? ' active' : ''}`} onClick={() => setIsMenuOpen(false)}>
                                        <Icon size={14} className="nav-item-icon" /> {label}
                                    </Link>
                                ))}
                            </>
                        )}
                        {isNavCollapsed && (
                            <div className="nav-more" ref={moreMenuRef}>
                                <button
                                    type="button"
                                    className={`nav-item nav-more-toggle${isMoreActive ? ' active' : ''}`}
                                    onClick={() => setIsMoreOpen((prev) => !prev)}
                                    aria-expanded={isMoreOpen}
                                >
                                    更多 <ChevronDown size={13} className="nav-item-icon" />
                                </button>
                                {isMoreOpen && (
                                    <div className="nav-more-menu">
                                        {MORE_NAV_ITEMS.map(({ to, label, Icon }) => (
                                            <Link key={to} to={to} className={`nav-item nav-more-link${isNavActive(to) ? ' active' : ''}`} onClick={() => setIsMoreOpen(false)}>
                                                <Icon size={14} className="nav-item-icon" /> {label}
                                            </Link>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}
                    </nav>

                    <div className="navbar-right">
                        <form className="navbar-search" onSubmit={handleSearch}>
                            <input
                                type="text"
                                placeholder="搜索..."
                                value={searchQuery}
                                onChange={(e) => setSearchQuery(e.target.value)}
                            />
                            <button type="submit"><Search size={16} /></button>
                        </form>

                        <div className="navbar-icons">
                            <button
                                onClick={toggleTheme}
                                className="nav-icon-btn theme-toggle"
                                title={isDarkMode ? "切换到浅色模式" : "切换到深色模式"}
                            >
                                {isDarkMode ? <Sun size={18} /> : <Moon size={18} />}
                            </button>

                            <div className="nav-icon-wrapper">
                                <button
                                    onClick={() => setIsLayoutOpen(!isLayoutOpen)}
                                    className={`nav-icon-btn ${isLayoutOpen ? 'active' : ''}`}
                                    title="布局设置"
                                >
                                    <Columns size={18} />
                                </button>

                                {isLayoutOpen && (
                                    <div className="layout-popover">
                                        <div className="popover-header">
                                            <span>卡片尺寸</span>
                                                <span className="popover-value">{dragCardWidth}px</span>
                                        </div>
                                        <div className="layout-mode-row">
                                            <span>布局模式</span>
                                            <span>{LAYOUT_MODE_LABELS[layoutMode] || LAYOUT_MODE_LABELS.default}</span>
                                        </div>
                                        <div className="layout-slider-container">
                                            <input
                                                type="range"
                                                min={CARD_WIDTH_MIN}
                                                max={CARD_WIDTH_MAX}
                                                step="10"
                                                value={dragCardWidth}
                                                onInput={handleCardWidthChange}
                                                onChange={handleCardWidthChange}
                                                onPointerDown={handleSliderMouseDown}
                                                onPointerUp={handleSliderMouseUp}
                                                onMouseDown={handleSliderMouseDown}
                                                onMouseUp={handleSliderMouseUp}
                                                onTouchStart={handleSliderMouseDown}
                                                onTouchEnd={handleSliderMouseUp}
                                                onBlur={commitCardWidth}
                                                className="layout-slider"
                                            />
                                        </div>
                                    </div>
                                )}
                            </div>
                            <button
                                onClick={() => {
                                    if (location.pathname === '/') {
                                        window.dispatchEvent(new Event('bus-terminal:refresh-home'));
                                    } else {
                                        window.location.reload();
                                    }
                                }}
                                className="nav-icon-btn nav-refresh-btn"
                                title="刷新当前页面"
                                aria-label="刷新当前页面"
                                type="button"
                            >
                                <RefreshCw size={17} />
                            </button>
                            <Link to="/settings" className="nav-icon-link" title="系统设置" onClick={() => setIsMenuOpen(false)}>
                                <Settings size={20} />
                            </Link>

                            {showLogout && (
                                <button onClick={handleLogout} className="nav-icon-btn" title="退出登录">
                                    <LogOut size={18} />
                                </button>
                            )}

                            <button className={`mobile-menu-toggle ${isMenuOpen ? 'active' : ''}`} onClick={toggleMenu}>
                                <span className="bar"></span>
                                <span className="bar"></span>
                                <span className="bar"></span>
                            </button>
                        </div>
                    </div>
                </div>
            </div>

        </header>
    );
}

export default Navbar;

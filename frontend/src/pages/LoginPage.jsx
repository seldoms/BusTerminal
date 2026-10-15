import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Lock } from 'lucide-react';
import { authApi, setAuthToken, getApiErrorMessage, logSafeError } from '../api';
import './LoginPage.css';

function LoginPage() {
    const navigate = useNavigate();
    const [checking, setChecking] = useState(true);
    const [isSetupMode, setIsSetupMode] = useState(false);
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [confirmPassword, setConfirmPassword] = useState('');
    const [error, setError] = useState('');
    const [submitting, setSubmitting] = useState(false);

    // 查询是否需要首次设置管理员
    useEffect(() => {
        let cancelled = false;
        authApi.getStatus()
            .then((res) => {
                if (!cancelled && res?.success && res.data?.setup_required) {
                    setIsSetupMode(true);
                }
            })
            .catch((err) => {
                // status 不可用时保持登录模式，不阻塞页面
                logSafeError('Auth status check failed', err);
            })
            .finally(() => {
                if (!cancelled) setChecking(false);
            });
        return () => { cancelled = true; };
    }, []);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');

        const trimmedUsername = username.trim();
        if (!trimmedUsername) {
            setError('请输入用户名');
            return;
        }
        if (!password) {
            setError('请输入密码');
            return;
        }
        if (isSetupMode) {
            if (password.length < 8) {
                setError('密码长度至少 8 位');
                return;
            }
            if (password !== confirmPassword) {
                setError('两次输入的密码不一致');
                return;
            }
        }

        setSubmitting(true);
        try {
            const res = isSetupMode
                ? await authApi.setup(trimmedUsername, password)
                : await authApi.login(trimmedUsername, password);
            const token = res?.data?.token;
            if (!res?.success || !token) {
                setError(res?.message || '操作失败，请稍后重试');
                return;
            }
            setAuthToken(token);
            navigate('/', { replace: true });
        } catch (err) {
            logSafeError(isSetupMode ? 'Setup failed' : 'Login failed', err);
            if (!isSetupMode && err?.response?.status === 401) {
                setError('用户名或密码错误');
            } else if (isSetupMode && err?.response?.status === 403) {
                setError('管理员账户已存在，请直接登录');
            } else {
                setError(getApiErrorMessage(err, '操作失败，请稍后重试'));
            }
        } finally {
            setSubmitting(false);
        }
    };

    if (checking) {
        return <div className="login-loading">加载中...</div>;
    }

    return (
        <div className="login-page">
            <div className="login-container">
                <div className="login-header">
                    <span className="login-icon"><Lock size={48} /></span>
                    <h1>{isSetupMode ? '创建管理员账户' : '登录 BusTerminal'}</h1>
                    <p>{isSetupMode ? '首次使用，请设置管理员账户' : '请输入账户信息以继续'}</p>
                </div>
                <form className="login-form" onSubmit={handleSubmit}>
                    <div className="form-group">
                        <label htmlFor="login-username">用户名</label>
                        <input
                            id="login-username"
                            type="text"
                            value={username}
                            onChange={(e) => setUsername(e.target.value)}
                            autoComplete="username"
                            autoFocus
                        />
                    </div>
                    <div className="form-group">
                        <label htmlFor="login-password">密码</label>
                        <input
                            id="login-password"
                            type="password"
                            value={password}
                            onChange={(e) => setPassword(e.target.value)}
                            autoComplete={isSetupMode ? 'new-password' : 'current-password'}
                        />
                    </div>
                    {isSetupMode && (
                        <div className="form-group">
                            <label htmlFor="login-confirm-password">确认密码</label>
                            <input
                                id="login-confirm-password"
                                type="password"
                                value={confirmPassword}
                                onChange={(e) => setConfirmPassword(e.target.value)}
                                autoComplete="new-password"
                            />
                        </div>
                    )}
                    {error && <div className="login-error">{error}</div>}
                    <button type="submit" className="btn btn-primary login-btn" disabled={submitting}>
                        {submitting
                            ? '提交中...'
                            : (isSetupMode ? '创建并登录' : '登录')}
                    </button>
                </form>
            </div>
        </div>
    );
}

export default LoginPage;

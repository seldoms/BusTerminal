import React, { useState } from 'react';
import { Eye, EyeOff } from 'lucide-react';

export default function SecretInput({
    value,
    onChange,
    placeholder,
    autoComplete = 'off',
    name,
    id,
    disabled,
    onBlur,
    className = 'settings-input',
    title,
}) {
    const [visible, setVisible] = useState(false);

    return (
        <div className="secret-field">
            <input
                type={visible ? 'text' : 'password'}
                className={className}
                value={value}
                onChange={onChange}
                placeholder={placeholder}
                autoComplete={autoComplete}
                name={name}
                id={id}
                disabled={disabled}
                onBlur={onBlur}
                title={title}
            />
            <button
                type="button"
                className="secret-field__toggle"
                aria-label={visible ? '隐藏密码' : '显示密码'}
                onClick={() => setVisible((v) => !v)}
            >
                {visible ? <EyeOff size={15} strokeWidth={1.5} /> : <Eye size={15} strokeWidth={1.5} />}
            </button>
        </div>
    );
}

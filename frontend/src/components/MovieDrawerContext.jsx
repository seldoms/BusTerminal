import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';

const MovieDrawerStateContext = createContext({
    isOpen: false,
    drawerData: null,
});

const MovieDrawerDispatchContext = createContext({
    openMovieDrawer: () => {},
    closeMovieDrawer: () => {},
});

export function MovieDrawerProvider({ children }) {
    const [isOpen, setIsOpen] = useState(false);
    const [drawerData, setDrawerData] = useState(null);

    const openMovieDrawer = useCallback((movieData) => {
        if (!movieData) return;
        setDrawerData(movieData);
        setIsOpen(true);
    }, []);

    const closeMovieDrawer = useCallback(() => {
        setIsOpen(false);
    }, []);

    const stateValue = useMemo(() => ({ isOpen, drawerData }), [isOpen, drawerData]);
    const dispatchValue = useMemo(() => ({ openMovieDrawer, closeMovieDrawer }), [openMovieDrawer, closeMovieDrawer]);

    return (
        <MovieDrawerStateContext.Provider value={stateValue}>
            <MovieDrawerDispatchContext.Provider value={dispatchValue}>
                {children}
            </MovieDrawerDispatchContext.Provider>
        </MovieDrawerStateContext.Provider>
    );
}

export function useMovieDrawerState() {
    return useContext(MovieDrawerStateContext);
}

export function useMovieDrawerDispatch() {
    return useContext(MovieDrawerDispatchContext);
}

export function useMovieDrawer() {
    const state = useContext(MovieDrawerStateContext);
    const dispatch = useContext(MovieDrawerDispatchContext);
    return { ...state, ...dispatch };
}

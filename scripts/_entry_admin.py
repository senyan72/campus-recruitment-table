from app.main import main
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main(["--mode", "admin"]))

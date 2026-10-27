THEME = """
QWidget {
    background-color: #080D18;
    color: #E8EEF8;
    font-family: "Segoe UI", sans-serif;
    font-size: 10pt;
}
QFrame#sidebar, QFrame#header, QFrame#composer {
    background-color: #0D1526;
}
QFrame#sidebar {
    border-right: 1px solid #24334D;
}
QLabel#brand {
    color: #60A5FA;
    font-size: 19pt;
    font-weight: 700;
}
QLabel#subtitle, QLabel#muted {
    color: #94A3B8;
}
QTextBrowser {
    background-color: #080D18;
    border: none;
    padding: 20px;
}
QLineEdit, QComboBox, QPlainTextEdit {
    background-color: #121E33;
    border: 1px solid #24334D;
    border-radius: 5px;
    padding: 10px;
    selection-background-color: #3B82F6;
}
QPushButton {
    background-color: #121E33;
    border: 1px solid #24334D;
    border-radius: 5px;
    padding: 9px 12px;
}
QPushButton:hover {
    background-color: #1A2A45;
    border-color: #3B82F6;
}
QPushButton#primary {
    background-color: #2563EB;
    border-color: #3B82F6;
    font-weight: 600;
}
QPushButton#primary:hover {
    background-color: #3B82F6;
}
QPushButton:disabled, QComboBox:disabled {
    color: #64748B;
}
QListWidget {
    background-color: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    border-radius: 4px;
    padding: 8px;
}
QListWidget::item:selected {
    background-color: #1A2A45;
    color: #E8EEF8;
}
QProgressBar {
    background-color: #1A2A45;
    border: none;
    border-radius: 4px;
}
QProgressBar::chunk {
    background-color: #3B82F6;
    border-radius: 4px;
}
QScrollBar:vertical {
    background: #080D18;
    width: 9px;
}
QScrollBar::handle:vertical {
    background: #24334D;
    min-height: 24px;
}
"""

"""Small standard-library HTML tree for rendered retail product cards."""
from html.parser import HTMLParser


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def find_all(self, predicate):
        for child in self.children:
            if isinstance(child, Node):
                if predicate(child):
                    yield child
                yield from child.find_all(predicate)

    def text(self):
        return ''.join(child.text() if isinstance(child, Node) else child for child in self.children).strip()

    def has_class(self, name):
        return name in self.attrs.get('class', '').split()


class _Tree(HTMLParser):
    VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def parse(content):
    parser = _Tree()
    parser.feed(content or '')
    return parser.root


def first(node, predicate):
    return next(node.find_all(predicate), None)

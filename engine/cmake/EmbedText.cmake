# cmake -DINPUT=<file> -DOUTPUT=<header> -DNAME=<symbol> -P EmbedText.cmake
# Writes the file's text as a C++ raw string constant.
file(READ "${INPUT}" content)
file(WRITE "${OUTPUT}" "#pragma once\ninline constexpr char const ${NAME}[] = R\"FXEMBED(${content})FXEMBED\";\n")
